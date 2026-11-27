"""EM-11 model comparison, analysis stages: ``analyze`` and ``sanity`` (see em11_compare.py).

``analyze`` reads the score banks and answers three questions, in this order:

1. Without any threshold, how well does each model rank a reaction's clips above the others?
   Average precision (AP) per reaction on the eval clips, with a paired bootstrap interval for
   the difference from YAMNet.
2. Which level would each model use? Chosen on dev clips only, with em11_eval.py's rule: find
   as many of the reaction's clips as possible while no other group opens it on more than 10 %
   of its clips (clips that carry the reaction's own label do not count against it).
3. With those levels, what happens on the eval clips when they go through ServerSession, the
   way em11_eval.py plays them: found, wrong reaction, false events per minute of speech and
   music, events per hour of everyday sound, while only playback sounds, and delay.

The server's detector knows laughter, scream and gasp. For this analysis only, the detector
module's reaction list is widened to the six reactions inside the analysis processes.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import statistics
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import em11_compare as core
import em11_eval as em11
from encore_server.audio import WINDOW_MS
from encore_server.detector import DetectorConfig

LEVELS = (.01, .02, .03, .04, .05, .06, .07, .08, .09, .1, .125, .15, .175, .2, .225, .25, .275, .3,
          .35, .4, .45, .5, .6, .7, .8, .9)
NEVER = 1.01  # a level no score reaches: used when no level is allowed on dev
MAX_FALSE = em11.MAX_FALSE
BOOT = 2000


# ---- metrics ------------------------------------------------------------------------------

def average_precision(y, s) -> float:
    """Step-wise AP (no interpolation), as scikit-learn computes it."""
    y, s = np.asarray(y, bool), np.asarray(s, float)
    if not y.any():
        return float("nan")
    order = np.argsort(-s, kind="mergesort")
    y, s = y[order], s[order]
    last = np.r_[np.where(np.diff(s))[0], len(s) - 1]
    tps = np.cumsum(y)[last]
    precision = tps / (last + 1)
    recall = tps / tps[-1]
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def roc_auc(y, s) -> float:
    y, s = np.asarray(y, bool), np.asarray(s, float)
    pos, neg = y.sum(), (~y).sum()
    if not pos or not neg:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s))
    sorted_s = s[order]
    i = 0
    while i < len(s):  # average ranks over ties
        j = i
        while j + 1 < len(s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[y].sum() - pos * (pos + 1) / 2) / (pos * neg))


def bootstrap_ap_diff(y, a, b, reps: int = BOOT, seed: int = core.SEED) -> tuple[float, float]:
    """95 % interval of AP(b) - AP(a) over clips resampled with replacement, positives and
    negatives separately, the same clips for both models."""
    y = np.asarray(y, bool)
    pos, neg = np.where(y)[0], np.where(~y)[0]
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(reps):
        idx = np.r_[rng.choice(pos, len(pos)), rng.choice(neg, len(neg))]
        diffs.append(average_precision(y[idx], b[idx]) - average_precision(y[idx], a[idx]))
    return float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def mcnemar(found_a: list[bool], found_b: list[bool]) -> dict:
    """Exact two-sided McNemar test on paired found/missed outcomes."""
    b = sum(x and not y for x, y in zip(found_a, found_b))
    c = sum(y and not x for x, y in zip(found_a, found_b))
    n = b + c
    p = 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, k) for k in range(min(b, c) + 1)) / 2 ** n)
    return {"only_a": b, "only_b": c, "p": round(p, 4)}


def _r(x: float, digits: int = 3):
    """Rounded, or None where there is no number (NaN): report.json stays strict JSON."""
    return None if x is None or math.isnan(x) else round(float(x), digits)


def wilson(k: int, n: int) -> tuple[float, float]:
    if not n:
        return float("nan"), float("nan")
    z, p = 1.96, k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


# ---- scores per clip ----------------------------------------------------------------------

def clip_window_keys(clips: core.Clips, selection: dict, sets) -> dict[str, list[str]]:
    """split/fname -> keys of the windows that touch the clip, played alone, for every clip of ``sets``."""
    out = {}
    for s in core.streams(selection, [x for x in sets if x != "mixed"]):
        length = clips.pcm(s.split, s.fname).size * 1000 // em11.RATE
        out[f"{s.split}/{s.fname}"] = [core.window_key(w.pcm) for w in core.touching(list(core.stream_windows(clips.stream_pcm(s))), length)]
    return out


def clip_max(keys: dict[str, list[str]], table: dict) -> dict[str, np.ndarray]:
    """split/fname -> the highest score of each reaction over the clip's windows."""
    zero = np.zeros(len(core.REACTIONS), np.float32)
    return {clip: np.max([table[k] for k in ks], axis=0) if ks else zero for clip, ks in keys.items()}


def opens(score, level):
    """The detector's test: the score rounded to 3 decimals, at or above the level (detector.py)."""
    if np.ndim(score):
        return np.round(np.asarray(score, np.float64), 3) >= level
    return round(float(score), 3) >= level


def choose_levels(dev: dict[str, np.ndarray], selection: dict, labels: dict) -> dict:
    """Per reaction: the dev-chosen level and the scan behind it (module docstring, question 2).
    Ties as em11_eval.choose: most found, then fewest false opens, then the highest level."""
    groups = selection["dev"]["groups"]
    out = {}
    for i, r in enumerate(core.REACTIONS):
        rows = []
        for level in LEVELS:
            hit = sum(opens(dev[f"dev/{f}"][i], level) for f in groups.get(r, []))
            opened, share = {}, {}
            for g, ids in groups.items():
                if g == r:
                    continue
                neg = [f for f in ids if r not in core.reactions_of(labels[f"dev/{f}"])]
                if neg:
                    opened[g] = int(sum(opens(dev[f"dev/{f}"][i], level) for f in neg))
                    share[g] = opened[g] / len(neg)
            worst = max(share, key=share.get) if share else None
            rows.append({"level": level, "found": int(hit), "of": len(groups.get(r, [])),
                         "worst_false": round(share[worst], 3) if worst else 0.0, "worst_group": worst,
                         "false_opens": sum(opened.values()),
                         "allowed": all(v <= MAX_FALSE for v in share.values())})
        allowed = [x for x in rows if x["allowed"]]
        best = max(allowed, key=lambda x: (x["found"], -x["false_opens"], x["level"])) if allowed else None
        out[r] = {"level": best["level"] if best else NEVER, "chosen": best, "scan": rows}
    return out


def thresholdless(scores: dict[str, dict[str, np.ndarray]], selection: dict, labels: dict, base: str = "yamnet") -> dict:
    """AP and ROC AUC per variant and reaction on the curated eval clips, and AP with the
    everyday clips added as negatives; paired bootstrap of each AP difference from ``base``."""
    curated = [f"eval/{f}" for ids in selection["eval"]["groups"].values() for f in ids]
    everyday = [f"eval/{f}" for f in selection["eval"].get(core.EVERYDAY, [])]
    out: dict = {}
    for name, table in scores.items():
        res = {}
        for i, r in enumerate(core.REACTIONS):
            entry = {}
            for label, keys in (("curated", curated), ("with_everyday", curated + everyday)):
                keys = [k for k in keys if k in table]
                y = np.array([r in core.reactions_of(labels[k]) for k in keys])
                s = np.array([table[k][i] for k in keys])
                entry[label] = {"ap": _r(average_precision(y, s)), "auc": _r(roc_auc(y, s)),
                                "positives": int(y.sum()), "clips": len(keys)}
                if name != base and base in scores and y.any():
                    common = [k for k in keys if k in scores[base]]
                    yc = np.array([r in core.reactions_of(labels[k]) for k in common])
                    a = np.array([scores[base][k][i] for k in common])
                    b = np.array([table[k][i] for k in common])
                    diff = average_precision(yc, b) - average_precision(yc, a)
                    lo, hi = bootstrap_ap_diff(yc, a, b)
                    entry[label]["vs_base"] = {"diff": round(diff, 3), "ci95": [round(lo, 3), round(hi, 3)]}
            res[r] = entry
        out[name] = res
    return out


# ---- the server path ----------------------------------------------------------------------

class LookupModel:
    """Plays back a bank's scores as the model, charging ``latency_ms`` per window to the delay."""

    def __init__(self, table: dict, latency_ms: float, name: str):
        self.table, self.latency, self.name, self.replayed_ms = table, latency_ms, name, 0.0

    def __call__(self, pcm: bytes) -> dict[str, float]:
        row = self.table[core.window_key(pcm)]  # KeyError: the bank misses a window
        self.replayed_ms += self.latency
        return {r: float(v) for r, v in zip(core.REACTIONS, row)}


class _ReplayClock:
    """em11_eval.run_stream adds each chunk's wall time to the delay. In a replay that time is the
    replay's own overhead, not the model's, so it counts as zero: the delay then holds the
    charged time per window only, and two runs give the same numbers."""

    perf_counter = staticmethod(lambda: 0.0)
    time = staticmethod(time.time)


@contextmanager
def replaying():
    """Inside this block the detector tracks all six reactions (analysis only) and run_stream's
    clock is the replay clock; both are put back afterwards."""
    import encore_server.detector as detector

    reactions, clock = detector.REACTIONS, em11.time
    detector.REACTIONS, em11.time = core.REACTIONS, _ReplayClock
    try:
        yield
    finally:
        detector.REACTIONS, em11.time = reactions, clock


_JOBS: dict = {}  # filled before the worker processes fork


def judge(opens: list, length_ms: int, onset: int) -> dict:
    first = em11.T0 + em11.LEAD_MS - WINDOW_MS // 2
    last = em11.T0 + em11.LEAD_MS + length_ms + WINDOW_MS // 2
    during = [(ev, t) for ev, t in opens if first <= ev["capture"]["triggers_ms"][0] <= last]
    outside = [ev["reaction"]["type"] for ev, t in opens if not first <= ev["capture"]["triggers_ms"][0] <= last]
    delay = {}
    for ev, t in during:
        r = ev["reaction"]["type"]
        d = round(t - (em11.T0 + em11.LEAD_MS + onset), 1)
        delay[r] = min(delay.get(r, d), d)
    return {"opened": sorted({ev["reaction"]["type"] for ev, _ in during}),
            "events": dict(Counter(ev["reaction"]["type"] for ev, _ in during)), "outside": outside, "delay_ms": delay}


def _replay(task):
    config_name, start, stop = task
    job = _JOBS[config_name]
    clips: core.Clips = _JOBS["_clips"]
    model = LookupModel(job["table"], job["latency_ms"], job["model_name"])
    config = DetectorConfig(name=config_name, trigger=dict(job["levels"]))
    rows = []
    for s in _JOBS["_streams"][start:stop]:
        clip = clips.pcm(s.split, s.fname)
        with replaying():
            opens, _ = em11.run_stream(clips.stream_pcm(s), model, face_server=False, config=config)
        row = judge(opens, clip.size * 1000 // em11.RATE, em11.onset_ms(clip))
        row.update(config=config_name, split=s.split, fname=s.fname, group=s.group, condition=s.condition,
                   background=s.background, snr_db=s.snr_db, length_ms=clip.size * 1000 // em11.RATE)
        rows.append(row)
    return rows


def replay(configs: dict, clips: core.Clips, selection: dict, workers: int) -> list[dict]:
    streams = core.streams(selection, ["curated", "mixed", "everyday"])
    _JOBS.clear()
    _JOBS.update(configs)
    _JOBS["_clips"], _JOBS["_streams"] = clips, streams
    step = max(1, math.ceil(len(streams) / (workers * 4)))
    tasks = [(name, i, min(i + step, len(streams))) for name in configs for i in range(0, len(streams), step)]
    if workers <= 1:
        return [row for t in tasks for row in _replay(t)]
    with multiprocessing.get_context("fork").Pool(workers) as pool:
        return [row for rows in pool.imap(_replay, tasks) for row in rows]


def summarize(rows: list[dict], labels: dict) -> dict:
    """Per configuration: found, wrong reactions, false events, delay (module docstring, question 3)."""
    out = {}
    by_config = defaultdict(list)
    for r in rows:
        by_config[r["config"]].append(r)
    for name, rs in by_config.items():
        clean = [r for r in rs if r["condition"] == "clean" and r["group"] != core.EVERYDAY]
        mixed = [r for r in rs if r["condition"] != "clean"]
        every = [r for r in rs if r["group"] == core.EVERYDAY]
        res: dict = {"found": {}, "found_mixed": {}, "wrong": {}, "opened_on": {}, "delay": {}}
        for reaction in core.REACTIONS:
            sel = [r for r in clean if r["group"] == reaction]
            k = sum(reaction in r["opened"] for r in sel)
            lo, hi = wilson(k, len(sel))
            res["found"][reaction] = {"k": k, "n": len(sel), "ci95": [_r(lo), _r(hi)] if sel else None,
                                      "found_fnames": sorted(r["fname"] for r in sel if reaction in r["opened"])}
            res["wrong"][reaction] = sum(any(o not in core.reactions_of(labels[f"eval/{r['fname']}"]) for o in r["opened"])
                                         for r in sel)
            delays = sorted(r["delay_ms"][reaction] for r in sel if reaction in r["delay_ms"])
            res["delay"][reaction] = ({"median_s": round(statistics.median(delays) / 1000, 2),
                                       "p90_s": round(delays[min(len(delays) - 1, math.ceil(.9 * len(delays)) - 1)] / 1000, 2),
                                       "n": len(delays)} if delays else None)
            for cond in sorted({r["condition"] for r in mixed}):
                m = [r for r in mixed if r["group"] == reaction and r["condition"] == cond]
                if m:
                    res["found_mixed"].setdefault(reaction, {})[cond] = {"k": sum(reaction in r["opened"] for r in m), "n": len(m)}
        for g in list(core.GROUPS) + [core.EVERYDAY]:
            sel = [r for r in (every if g == core.EVERYDAY else clean) if r["group"] == g]
            if sel:
                res["opened_on"][g] = {"n": len(sel), **{x: sum(x in r["opened"] for r in sel) for x in core.REACTIONS}}
        for g in core.OTHER_GROUPS + (core.EVERYDAY,):
            sel = [r for r in (every if g == core.EVERYDAY else clean) if r["group"] == g]
            seconds = sum(r["length_ms"] for r in sel) / 1000
            events = Counter()
            for r in sel:
                events.update(r["events"])
            res.setdefault("false", {})[g] = {"events": dict(events), "total": sum(events.values()),
                                              "seconds": round(seconds), "per_minute": round(60 * sum(events.values()) / seconds, 2) if seconds else None,
                                              "per_hour": round(3600 * sum(events.values()) / seconds, 1) if seconds else None}
        alone = sum(len(r["outside"]) for r in mixed)
        alone_s = len(mixed) * (em11.LEAD_MS + em11.TAIL_MS) / 1000
        res["playback_alone"] = {"events": alone, "seconds": alone_s, "per_minute": round(60 * alone / alone_s, 2) if alone_s else None}
        out[name] = res
    return out


# ---- the report ---------------------------------------------------------------------------

def _pct(k, n):
    return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "—"


def report(result: dict) -> str:
    m = result["data"]
    names = list(result["levels"])
    lines = [
        "# EM-11 model comparison: results\n",
        f"Created {result['created']}. Data: {m['clips']} prepared clips ({m['eval_hours']} h of eval audio), "
        f"copy used: {m['copy_used']}, sources {m['sources']}. Code: {result.get('commit') or 'see notebook log'}.",
        "Levels are chosen on dev clips; every number below is on eval clips.\n",
        "## 1. Ranking without thresholds (eval, clean clips)\n",
        "Average precision (AP) per reaction. *Curated*: the reaction and living-room clips; *+ everyday*: "
        "with every other eval clip added as a negative. In brackets: 95 % interval of the difference from YAMNet.\n",
        "| Reaction | Positives | " + " | ".join(result["thresholdless"]) + " |",
        "|---|---|" + "---|" * len(result["thresholdless"]),
    ]
    for r in core.REACTIONS:
        for label in ("curated", "with_everyday"):
            cells = []
            for name, res in result["thresholdless"].items():
                e = res[r][label]
                vs = e.get("vs_base")
                cells.append(("—" if e["ap"] is None else f"{e['ap']:.3f}") + (f" ({vs['diff']:+.3f}, {vs['ci95'][0]:+.3f}…{vs['ci95'][1]:+.3f})" if vs else ""))
            first = next(iter(result["thresholdless"].values()))[r][label]
            lines.append(f"| {r}{' + everyday' if label == 'with_everyday' else ''} | {first['positives']} | " + " | ".join(cells) + " |")
    lines += ["\n## 2. Levels chosen on dev clips\n",
              "Most dev clips found while no other dev group opens the reaction on more than 10 % of its clips. "
              "`never`: no level met that.\n",
              "| Reaction | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for r in core.REACTIONS:
        cells = []
        for name in names:
            c = result["levels"][name][r]["chosen"]
            cells.append(f"{c['level']:g} ({c['found']}/{c['of']} dev, worst other {c['worst_false']:.0%} {c['worst_group']})"
                         if c else "never")
        lines.append(f"| {r} | " + " | ".join(cells) + " |")
    s = result["server"]
    cfgs = list(s)
    lines += ["\n## 3. Eval clips through ServerSession at those levels\n",
              "`yamnet@v3` is the server today (detector-v3 levels for laughter, scream and gasp; YAMNet's dev levels for "
              "the candidates). p: exact McNemar test against `yamnet@dev` on the same clips.\n",
              "### Found, clean\n", "| Reaction | " + " | ".join(cfgs) + " |", "|---|" + "---|" * len(cfgs)]
    for r in core.REACTIONS:
        cells = []
        for c in cfgs:
            f = s[c]["found"][r]
            p = result["mcnemar"].get(c, {}).get(r)
            cells.append(_pct(f["k"], f["n"]) + (f" p={p['p']}" if p and f["n"] else ""))
        lines.append(f"| {r} | " + " | ".join(cells) + " |")
    lines += ["\n### Found over playback (+10 dB / 0 dB)\n", "| Reaction | Playback | " + " | ".join(cfgs) + " |",
              "|---|---|" + "---|" * len(cfgs)]
    for r in core.REACTIONS:
        for bg in ("speech", "music"):
            cells = []
            for c in cfgs:
                fm = s[c]["found_mixed"].get(r, {})
                a, b = fm.get(f"{bg}+10dB"), fm.get(f"{bg}+0dB")
                cells.append(f"{a['k']} / {b['k']} of {a['n']}" if a and b else "—")
            lines.append(f"| {r} | {bg} | " + " | ".join(cells) + " |")
    lines += ["\n### False events and wrong reactions\n",
              "| | " + " | ".join(cfgs) + " |", "|---|" + "---|" * len(cfgs)]
    for g, unit in (("speech", "per_minute"), ("music", "per_minute"), ("breath_cough", "per_minute"), (core.EVERYDAY, "per_hour")):
        lines.append(f"| {g}, events {unit.replace('_', ' ')} | " + " | ".join(
            f"{s[c]['false'][g][unit]} ({s[c]['false'][g]['total']} in {s[c]['false'][g]['seconds']} s)" for c in cfgs) + " |")
    lines.append("| playback alone, events per minute | " + " | ".join(str(s[c]["playback_alone"]["per_minute"]) for c in cfgs) + " |")
    for r in core.REACTIONS:
        lines.append(f"| {r} clips that opened another reaction | " + " | ".join(str(s[c]["wrong"][r]) for c in cfgs) + " |")
    lines += ["\n### Which reaction opened on which clips (clean; share of the group's clips)\n"]
    for c in cfgs:
        lines += [f"\n`{c}`\n", "| Group | Clips | " + " | ".join(core.REACTIONS) + " |", "|---|---|" + "---|" * len(core.REACTIONS)]
        for g, o in s[c]["opened_on"].items():
            lines.append(f"| {g} | {o['n']} | " + " | ".join(f"{100 * o[x] / o['n']:.0f}%" for x in core.REACTIONS) + " |")
    lines += ["\n### Delay from the sound's onset to OPEN, median / p90 (clean, found clips)\n",
              "Includes the model's time per window: " + ", ".join(f"`{c}` {result['latency_ms'][c]} ms" for c in cfgs)
              + " (section 4; AST on the GPU).\n",
              "| Reaction | " + " | ".join(cfgs) + " |", "|---|" + "---|" * len(cfgs)]
    for r in core.REACTIONS:
        lines.append(f"| {r} | " + " | ".join(
            (f"{d['median_s']:.2f} / {d['p90_s']:.2f} s" if (d := s[c]["delay"][r]) else "—") for c in cfgs) + " |")
    if result.get("timing"):
        lines += ["\n## 4. Time per window (batch 1)\n", "| Model | Device | Threads | Precision | Median | p90 |",
                  "|---|---|---|---|---|---|"]
        for t in result["timing"]:
            lines.append(f"| {t['variant']} | {t['scorer'].get('gpu') or t['device']} | {t['threads'] or '—'} | {t['precision']} | "
                         f"{t['median_ms']} ms | {t['p90_ms']} ms |")
    if result.get("precision_check"):
        lines += ["\n## 5. Half against full precision\n", "| Model | Windows | Largest difference | Decisions flipped at the dev levels |",
                  "|---|---|---|---|"]
        for name, pc in result["precision_check"].items():
            lines.append(f"| {name} | {pc['windows']} | {pc['max_abs']:.4f} | {pc['flipped']} |")
    if result.get("sanity"):
        y = result["sanity"]
        lines += ["\n## 6. Sanity check: detector-v3 on the original 280 clips\n",
                  f"em11_eval.py on this machine against the committed rows: {y['rows']} rows, "
                  f"{y['mismatch_count']} differ ({y['model']}). Differences: {y['mismatches'][:10]}"]
    lines += ["\n## Run details\n", "| Bank | Rows | Model seconds | Per second | Precision | Device |", "|---|---|---|---|---|---|"]
    for name, metas in result["banks"].items():
        for b in metas:
            lines.append(f"| {name} | {b['rows']} | {b['model_seconds']} | {b['per_second']} | {b['precision']} | "
                         f"{b['scorer'].get('gpu') or b['scorer'].get('device', 'cpu')} |")
    return "\n".join(lines) + "\n"


# ---- stages -------------------------------------------------------------------------------

def charged_latency(timing: list[dict]) -> dict[str, float]:
    """Time per window added to each model's delay: YAMNet as the server runs it (CPU, 2 threads
    when measured), AST on a GPU when measured (on the server's CPU it would be far slower)."""
    out = {}
    for variant in {t["variant"] for t in timing}:
        runs = [t for t in timing if t["variant"] == variant]
        if variant == "yamnet":
            prefer = lambda t: (t["device"] == "cpu", t.get("threads") == 2)  # noqa: E731
        else:
            prefer = lambda t: (t["device"] != "cpu", t["precision"] == "fp32")  # noqa: E731
        out[variant] = max(runs, key=prefer)["median_ms"]
    return out


def _parse_banks(items: list[str]) -> dict[str, list[Path]]:
    out = {}
    for item in items:
        name, _, files = item.partition("=")
        if name not in core.VARIANTS or not files:
            raise SystemExit(f"--bank {item!r}: use VARIANT=FILE[,FILE...] with VARIANT one of {core.VARIANTS}")
        out[name] = [Path(f) for f in files.split(",")]
    return out


def analyze(args) -> dict:
    data = Path(args.data)
    manifest = json.loads((data / "manifest.json").read_text())
    selection = core.limited(manifest["selection"], args.limit)
    labels = manifest["labels"]
    clips = core.Clips(data, manifest)
    banks = _parse_banks(args.bank)
    tables, metas = {}, {}
    for name, files in banks.items():
        tables[name], metas[name] = core.load_bank(*files)
    timing = [json.loads(Path(t).read_text()) for t in args.timing]
    latency = charged_latency(timing)

    clip_scores, levels = {}, {}
    keys = clip_window_keys(clips, selection, ["curated", "dev", "everyday"]) if set(tables) - {"ast-clip"} else {}
    for name, table in tables.items():
        if name == "ast-clip":
            clip_scores[name] = table
            dev = {k: v for k, v in table.items() if k.startswith("dev/")}
        else:
            clip_scores[name] = clip_max(keys, table)
            dev = {k: v for k, v in clip_scores[name].items() if k.startswith("dev/")}
        levels[name] = choose_levels(dev, selection, labels)
        print(f"{name}: levels " + ", ".join(f"{r} {levels[name][r]['level']:g}" for r in core.REACTIONS), file=sys.stderr)
    eval_scores = {n: {k: v for k, v in t.items() if k.startswith("eval/")} for n, t in clip_scores.items()}
    base = "yamnet" if "yamnet" in tables else next(iter(tables))
    free = thresholdless(eval_scores, selection, labels, base)

    configs = {}
    for name, table in tables.items():
        if name == "ast-clip":
            continue
        model_name = metas[name][0]["scorer"]["model"]
        dev_levels = {r: levels[name][r]["level"] for r in core.REACTIONS}
        configs[f"{name}@dev"] = {"table": table, "levels": dev_levels, "latency_ms": latency.get(name, 0.0), "model_name": model_name}
        if name == "yamnet":
            v3 = DetectorConfig().trigger
            configs["yamnet@v3"] = {"table": table, "levels": dev_levels | v3, "latency_ms": latency.get(name, 0.0),
                                    "model_name": model_name}
    rows = replay(configs, clips, selection, args.workers)
    server = summarize(rows, labels)
    tests = {}
    base_cfg = f"{base}@dev"
    for c in configs:
        if c == base_cfg or base_cfg not in server:
            continue
        tests[c] = {}
        for r in core.REACTIONS:
            a = {x["fname"]: r in x["opened"] for x in rows if x["config"] == base_cfg and x["condition"] == "clean" and x["group"] == r}
            b = {x["fname"]: r in x["opened"] for x in rows if x["config"] == c and x["condition"] == "clean" and x["group"] == r}
            common = sorted(set(a) & set(b))
            tests[c][r] = mcnemar([a[f] for f in common], [b[f] for f in common])

    checks = {}
    for item in args.check:
        name, _, path = item.partition("=")
        half, _ = core.load_bank(Path(path))
        common = [k for k in half if k in tables.get(name, {})]
        if common:
            full = np.array([tables[name][k] for k in common])
            h = np.array([half[k] for k in common])
            lv = np.array([levels[name][r]["level"] for r in core.REACTIONS])
            checks[name] = {"windows": len(common), "max_abs": float(np.abs(full - h).max()),
                            "mean_abs": float(np.abs(full - h).mean()), "flipped": int((opens(full, lv) != opens(h, lv)).sum())}
    files = manifest["files"]
    result = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": getattr(args, "commit", None),
        "data": {"created": manifest["created"], "clips": len(files), "copy_used": manifest["checks"]["copy_used"],
                 "sources": manifest["sources"], "limit": args.limit or manifest.get("limit"),
                 "eval_hours": round(sum(v["samples"] for k, v in files.items() if k.startswith("eval/")) / em11.RATE / 3600, 1)},
        "banks": {n: m for n, m in metas.items()},
        "latency_ms": {c: job["latency_ms"] for c, job in configs.items()},
        "thresholdless": free, "levels": levels, "server": server, "mcnemar": tests, "timing": timing,
        "precision_check": checks,
        "sanity": json.loads(Path(args.sanity).read_text()) if args.sanity else None,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(result, indent=1, allow_nan=False) + "\n")
    (out / "rows.json").write_text(json.dumps(rows) + "\n")
    (out / "report.md").write_text(report(result))
    print(report(result))
    return result


def sanity(args) -> dict:
    """em11_eval.py on the original 280 clips, from this run's 16 kHz files; then the opened
    reactions of every row against docs/em-11-compare-sanity.json."""
    data, work = Path(args.data), Path(args.work)
    old = json.loads((core.REPO / "docs" / "em-11-clips.json").read_text())
    cache = work / "em11-280"
    (cache / "16k").mkdir(parents=True, exist_ok=True)
    for f in {f for ids in old["groups"].values() for f in ids}:
        link = cache / "16k" / f"{f}.wav"
        link.unlink(missing_ok=True)
        link.symlink_to((data / "16k" / "eval" / f"{f}.wav").resolve())
    (cache / "manifest.json").write_text(json.dumps({"groups": old["groups"]}))
    out_json = work / "em11-280.json"
    em11.main(["run", "--cache", str(cache), "--json", str(out_json)])
    got = json.loads(out_json.read_text())
    expected = json.loads(core.SANITY.read_text())
    opened = {f"{r['fname']}|{r['condition']}|{r['setup']}": r["opened"] for r in got["rows"]}
    keys = set(opened) | set(expected["opened"])
    diff = sorted(k for k in keys if sorted(opened.get(k, [])) != sorted(expected["opened"].get(k, [])))
    result = {"rows": len(got["rows"]), "expected_rows": expected["info"]["rows"], "model": got["info"]["model"],
              "expected_model": expected["info"]["model"], "mismatch_count": len(diff),
              "mismatches": [{"row": k, "got": opened.get(k, []), "expected": expected["opened"].get(k, [])} for k in diff[:50]],
              "info": got["info"]}
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({k: result[k] for k in ("rows", "expected_rows", "model", "mismatch_count")}))
    return result


def main(args) -> int:
    if args.cmd == "sanity":
        result = sanity(args)
        return 0 if result["mismatch_count"] == 0 and result["rows"] == result["expected_rows"] else 1
    analyze(args)
    return 0
