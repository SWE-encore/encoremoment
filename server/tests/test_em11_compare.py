"""EM-11 model comparison tool (server/tools/em11_compare*.py) without TensorFlow, torch or data.

Scripted score banks stand in for YAMNet and AST, so these check the tool itself: which clips go
into which group, that the windows it scores are the ones ServerSession makes, how banks merge,
the statistics, how levels are chosen on dev clips, and a whole analysis through the real
ServerSession. The prepare test needs ffmpeg and is skipped without it.
"""
import argparse
import json
import shutil
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import em11_compare as core  # noqa: E402
import em11_compare_analysis as analysis  # noqa: E402
import em11_compare_data as data  # noqa: E402
import em11_compare_models as models  # noqa: E402

em11 = core.em11
RATE = em11.RATE


def noise(seconds, level=.3, seed=1, rate=RATE):
    return (np.random.default_rng(seed).normal(0, level, int(seconds * rate)) * 32768).clip(-32768, 32767).astype(np.int16)


def write_wav(path: Path, pcm: np.ndarray, rate: int = RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def write_labels(folder: Path, split_labels: dict[str, dict[str, list[str]]]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for split, labels in split_labels.items():
        rows = ["fname,labels,mids"] + [f'{f},"{",".join(ls)}",x' for f, ls in labels.items()]
        (folder / f"{split}.csv").write_text("\n".join(rows) + "\n")
    return folder


# ---- choosing the clips -------------------------------------------------------------------

EVAL_LABELS = {
    "1": ["Laughter", "Human_voice"],
    "2": ["Laughter", "Screaming"],          # two reactions: in no group, and not everyday
    "3": ["Cheering", "Applause", "Crowd"],  # cheering first; also an applause positive
    "4": ["Applause"],
    "5": ["Shout", "Speech"],                # a shout, never plain speech
    "6": ["Speech"],
    "7": ["Music", "Guitar"],
    "8": ["Music", "Drum"],
    "9": ["Music", "Singing", "Human_voice"],  # no group, no reaction label: everyday
    "10": ["Dog", "Animal"],
    "11": ["Cough"],
    "12": ["Yell", "Applause"],              # applause comes first; also a shout positive
    "1791": ["Laughter"],                    # an ESC-50 clip EM-26 already ran
}
DEV_LABELS = {"20": ["Laughter"], "21": ["Speech"], "22": ["Dog"], "23": ["Car"], "24": ["Gasp", "Breathing"]}


def test_each_clip_joins_one_group_and_everyday_has_no_reaction(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "MUSIC_SAMPLE", 1)
    monkeypatch.setattr(core, "DEV_EVERYDAY", 1)
    labels = write_labels(tmp_path, {"eval": EVAL_LABELS, "dev": DEV_LABELS})
    sel = core.select(labels)
    ev = sel["eval"]
    assert {g: sorted(ids, key=int) for g, ids in ev["groups"].items() if ids} == {
        "laughter": ["1"], "cheering": ["3"], "applause": ["4", "12"], "shout": ["5"], "speech": ["6"],
        "music": ev["groups"]["music"], "breath_cough": ["11"]}
    assert len(ev["groups"]["music"]) == 1  # one music clip in the living-room set ...
    other_music = ({"7", "8"} - set(ev["groups"]["music"])).pop()
    assert ev[core.EVERYDAY] == sorted([other_music, "9", "10"], key=int)  # ... the other is everyday sound
    assert ev["counts"][core.EVERYDAY] == 3
    dev = sel["dev"]
    assert dev["groups"]["laughter"] == ["20"] and dev["groups"]["gasp"] == ["24"]
    assert len(dev["groups"][core.EVERYDAY]) == 1 and dev["groups"][core.EVERYDAY][0] in {"22", "23"}
    assert core.select(labels) == sel  # fixed by the seed
    assert core.reactions_of(EVAL_LABELS["3"]) == {"cheering", "applause"}
    assert core.reactions_of(EVAL_LABELS["12"]) == {"shout", "applause"}
    assert set(core.selected_ids(sel, "eval")) == {"1", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"}

    small = core.limited(sel, 1)
    assert small["eval"][core.EVERYDAY] == ev[core.EVERYDAY][:4] and small is not sel
    assert core.limited(sel, None) is sel


def test_streams_play_each_reaction_clip_over_speech_and_music():
    sel = {"eval": {"groups": {g: [] for g in core.GROUPS} | {"laughter": ["1", "2"], "cheering": ["3"],
                                                              "speech": ["6"], "music": ["7", "8"]},
                    core.EVERYDAY: ["10"]},
           "dev": {"groups": {"laughter": ["20"], core.EVERYDAY: ["22"]}}}
    all_streams = core.streams(sel)
    curated = [s for s in all_streams if s.split == "eval" and s.condition == "clean" and s.group != core.EVERYDAY]
    assert [s.fname for s in curated] == ["1", "2", "3", "6", "7", "8"]
    mixed = [s for s in all_streams if s.condition != "clean"]
    assert len(mixed) == 3 * 2 * len(em11.SNRS)  # three reaction clips, two kinds of playback
    assert {(s.fname, s.background) for s in mixed if s.background in ("7", "8")} == {("1", "7"), ("2", "8"), ("3", "7")}
    assert [s.key for s in all_streams if s.split == "dev"] == ["dev/20/clean", "dev/22/clean"]
    assert [s.fname for s in all_streams if s.group == core.EVERYDAY and s.split == "eval"] == ["10"]
    assert core.streams(sel, ["dev"]) == [s for s in all_streams if s.split == "dev"]


# ---- windows ------------------------------------------------------------------------------

class Recorder:
    """A model that records which windows the server asks it about."""

    name = "recorder"

    def __init__(self):
        self.keys, self.replayed_ms = [], 0.0

    def __call__(self, pcm):
        self.keys.append(core.window_key(pcm))
        return {r: 0.0 for r in core.PRODUCT}


def test_the_scored_windows_are_the_ones_serversession_makes():
    clip = noise(2.3, seed=7)
    stream = em11.clean_stream(clip)
    model = Recorder()
    _, frames = em11.run_stream(stream, model, face_server=False)
    ours = list(core.stream_windows(stream))
    assert [(w.start_ms, w.end_ms) for w in ours] == [(f.start_ms, f.end_ms) for f in frames]
    assert [core.window_key(w.pcm) for w in ours] == model.keys
    length = len(clip) * 1000 // RATE
    touching = core.touching(ours, length)
    start, end = em11.T0 + em11.LEAD_MS, em11.T0 + em11.LEAD_MS + length
    assert touching == [w for w in ours if w.end_ms > start and w.start_ms < end]
    assert 0 < len(touching) < len(ours)


def test_whole_clips_are_cut_into_full_ast_pieces():
    size = core.AST_SAMPLES
    assert models.clip_pieces(5_000) == [(0, 5_000)] and models.clip_pieces(size) == [(0, size)]
    # The clip that stopped the first full run: 127 samples past one piece.
    assert models.clip_pieces(size + 127) == [(0, size), (127, size + 127)]
    assert models.clip_pieces(2 * size) == [(0, size), (size, 2 * size)]
    for n in (size + 1, 2 * size + 1, 3 * size - 1, 5 * size + 399, 7 * size + 12_345):
        pieces = models.clip_pieces(n)
        assert all(b - a == size for a, b in pieces)  # never a short leftover
        assert pieces[0][0] == 0 and pieces[-1][1] == n
        assert all(a2 <= b1 for (_, b1), (a2, _) in zip(pieces, pieces[1:]))  # no gap between pieces
        assert len(pieces) == -(-n // size)
    assert models.FBANK_MIN == 400


def test_preflight_adds_the_clips_most_likely_to_break_a_stage():
    size = core.AST_SAMPLES
    groups = {g: [] for g in core.GROUPS}
    groups.update(laughter=["1", "2"], speech=["3", "4"], music=["5"])
    samples = {"1": 32_000, "2": size + 127, "3": 4_800, "4": 2 * size + 5_000, "5": 3 * size + 50_000,
               "6": 20_000, "7": 5 * size + 1_000, "8": 16_000}
    dev_groups = {g: [] for g in core.GROUPS} | {"laughter": ["21"], core.EVERYDAY: ["22", "23"]}
    manifest = {"selection": {"eval": {"groups": groups, core.EVERYDAY: ["6", "7", "8"]},
                              "dev": {"groups": dev_groups, core.EVERYDAY: []}},
                "files": {**{f"eval/{f}": {"samples": n} for f, n in samples.items()},
                          "dev/21": {"samples": 9_000}, "dev/22": {"samples": size + 400}, "dev/23": {"samples": 700}}}
    sel, extra = core.preflight(manifest, per_group=1)
    ev = sel["eval"]
    assert ev["groups"]["laughter"] == ["1", "2"]  # "2": the shortest last piece, kept in its own group
    assert ev["groups"]["speech"] == ["3"]         # the first speech clip is also the shortest clip
    assert ev[core.EVERYDAY] == ["6", "7", "8"] and ev["groups"]["music"] == ["5"]
    assert extra == {
        "eval/3": "shortest: 0.30 s, group speech",
        "eval/7": "longest, last piece 1000 samples: 51.26 s, group everyday",
        "eval/2": "last piece 127 samples: 10.25 s, group laughter",
        "dev/23": "shortest: 0.04 s, group everyday",
        "dev/22": "longest, last piece 400 samples: 10.27 s, group everyday"}
    assert sel["dev"]["groups"][core.EVERYDAY] == ["22", "23"]
    assert manifest["selection"]["eval"]["groups"]["laughter"] == ["1", "2"]  # the manifest is untouched
    assert core.chosen(manifest, 1) == core.limited(manifest["selection"], 1)


def test_position_embeddings_keep_the_middle_time_patches():
    freq, time_, keep, d = 12, 101, 9, 3
    index = np.arange(freq * time_)
    pe = np.concatenate([np.full((1, 2, d), -1.0), np.repeat(index[None, :, None], d, axis=2).astype(float)], 1)
    cut = models.cut_positions(pe, freq, time_, keep)
    assert cut.shape == (1, 2 + freq * keep, d)
    assert (cut[0, :2] == -1).all()
    got = cut[0, 2:, 0].reshape(freq, keep)
    assert (got == np.array([[f * time_ + t for t in range(46, 55)] for f in range(freq)])).all()


# ---- banks --------------------------------------------------------------------------------

def test_banks_round_trip_and_the_first_file_wins(tmp_path):
    a = np.array([[.1] * 6, [.2] * 6], np.float32)
    core.save_bank(tmp_path / "a.npz", ["k1", "k2"], a, {"variant": "x", "precision": "fp32"})
    core.save_bank(tmp_path / "b.npz", ["k2", "k3"], a + .5, {"variant": "x", "precision": "fp16"},
                   full=np.ones((2, 4)))
    table, metas = core.load_bank(tmp_path / "a.npz", tmp_path / "b.npz")
    assert sorted(table) == ["k1", "k2", "k3"]
    assert np.allclose(table["k2"], .2) and np.allclose(table["k3"], .7)
    assert [m["precision"] for m in metas] == ["fp32", "fp16"]
    with np.load(tmp_path / "b.npz") as z:
        assert z["full"].dtype == np.float16
    assert not list(tmp_path.glob("*.part.npz"))


def test_reaction_scores_take_the_highest_class_of_each_reaction():
    labels = sorted({n for names in core.CLASSES.values() for n in names} | {"Speech"})
    v = np.zeros((2, len(labels)), np.float32)
    v[0, labels.index("Giggle")] = .7
    v[0, labels.index("Laughter")] = .2
    v[1, labels.index("Clapping")] = .4
    v[1, labels.index("Speech")] = .9
    got = core.reaction_scores(v, labels)
    assert got.shape == (2, 6)
    assert np.allclose(got[0], [.7, 0, 0, 0, 0, 0]) and np.allclose(got[1], [0, 0, 0, 0, .4, 0])
    with pytest.raises(RuntimeError):
        core.reaction_scores(v[:, 1:], labels[1:])


# ---- a prepared data folder, for score and analyze ------------------------------------------

LABELS = {
    "eval/101": ["Laughter"], "eval/102": ["Laughter", "Giggle"], "eval/103": ["Screaming"],
    "eval/104": ["Cheering", "Applause"], "eval/105": ["Speech"], "eval/106": ["Speech"],
    "eval/107": ["Music"], "eval/108": ["Dog"],
    "dev/201": ["Laughter"], "dev/202": ["Laughter"], "dev/203": ["Screaming"],
    "dev/204": ["Cheering", "Applause"], "dev/205": ["Speech"], "dev/206": ["Speech"],
    "dev/207": ["Music"], "dev/208": ["Car"],
}
GROUP_OF = {"101": "laughter", "102": "laughter", "103": "scream", "104": "cheering", "105": "speech",
            "106": "speech", "107": "music", "108": core.EVERYDAY}


def make_data(folder: Path) -> Path:
    def groups(split, offset):
        g = {name: [] for name in core.GROUPS}
        for f, name in GROUP_OF.items():
            if name != core.EVERYDAY:
                g[name].append(str(int(f) + offset))
        return g

    selection = {"eval": {"groups": groups("eval", 0), core.EVERYDAY: ["108"]},
                 "dev": {"groups": groups("dev", 100) | {core.EVERYDAY: ["208"]}, core.EVERYDAY: []}}
    files = {}
    for i, key in enumerate(LABELS):
        split, fname = key.split("/")
        path = folder / "16k" / split / f"{fname}.wav"
        quiet = int(fname) % 100 in (5, 6, 7, 8)  # the other sounds are quieter
        write_wav(path, noise(1.6 + .1 * (i % 3), .05 if quiet else .3, seed=i))
        files[key] = {"source": "copy", "sha256_16k": core.sha256_file(path), "samples": path.stat().st_size // 2 - 22}
    manifest = {"created": "2026-11-27T00:00:00+00:00", "limit": None, "checks": {"copy_used": True},
                "sources": {"copy": len(files)}, "selection": selection, "labels": LABELS, "files": files}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return folder


SCRIPT = {  # group -> reaction scores on windows that touch the clip
    "yamnet": {"laughter": {"laughter": .5}, "scream": {"scream": .3}, "cheering": {"cheering": .4, "applause": .4},
               "speech": {"laughter": .05}},
    "ast-cut1s": {"laughter": {"laughter": .8}, "cheering": {"cheering": .6, "applause": .6},
                  "speech": {"laughter": .05}},
}


def scripted_bank(path: Path, folder: Path, variant: str, selection: dict | None = None) -> dict:
    """What the score stage would save for a model that scores by the clip's group."""
    manifest = json.loads((folder / "manifest.json").read_text())
    clips = core.Clips(folder, manifest)
    rule = SCRIPT[variant]
    table = {}
    for s in core.streams(selection or manifest["selection"]):
        clip = clips.pcm(s.split, s.fname)
        start, end = em11.T0 + em11.LEAD_MS, em11.T0 + em11.LEAD_MS + clip.size * 1000 // RATE
        for w in core.stream_windows(clips.stream_pcm(s)):
            row = np.zeros(6, np.float32)
            if w.end_ms > start and w.start_ms < end:
                for r, v in rule.get(s.group, {}).items():
                    row[core.REACTIONS.index(r)] = v
            table.setdefault(core.window_key(w.pcm), row)
    meta = {"variant": variant, "precision": "fp32", "rows": len(table), "model_seconds": 1.0, "per_second": len(table),
            "scorer": {"model": f"scripted-{variant}", "device": "cpu"}}
    core.save_bank(path, list(table), np.array(list(table.values())), meta)
    return table


class FakeScorer:
    kind = "window"
    labels = sorted({n for names in core.CLASSES.values() for n in names})
    meta = {"model": "fake"}

    def __call__(self, batch):
        out = np.zeros((len(batch), len(self.labels)), np.float32)
        for i, pcm in enumerate(batch):
            out[i, self.labels.index("Laughter")] = min(1.0, float(np.abs(np.frombuffer(pcm, "<i2")).mean()) / 10_000)
        return out


def test_shards_split_the_windows_and_skip_leaves_out_scored_ones(tmp_path, monkeypatch):
    folder = make_data(tmp_path / "data")
    monkeypatch.setattr(models, "make_scorer", lambda *a, **k: FakeScorer())

    def run(out, shard="0/1", sets="curated,mixed,dev,everyday", skip=()):
        argv = ["score", "--data", str(folder), "--model", "yamnet", "--sets", sets, "--out", str(tmp_path / out),
                "--shard", shard, "--batch", "7", "--threads", "1"] + [a for f in skip for a in ("--skip", str(f))]
        assert core.main(argv) == 0  # the command line the Kaggle notebook uses
        return core.load_bank(tmp_path / out)[0]

    whole = run("whole.npz")
    a, b = run("a.npz", shard="0/2"), run("b.npz", shard="1/2")
    assert a and b and not set(a) & set(b) and set(a) | set(b) == set(whole)
    assert all(np.array_equal(whole[k], v) for k, v in (a | b).items())
    rest = run("rest.npz", sets="curated,mixed,dev,everyday", skip=[tmp_path / "a.npz"])
    assert set(rest) == set(b)
    curated = run("curated.npz", sets="curated")
    assert set(curated) < set(whole)

    (folder / "16k" / "eval" / "101.wav").write_bytes(b"changed")
    with pytest.raises(SystemExit):
        run("bad.npz")


# ---- statistics ---------------------------------------------------------------------------

def test_average_precision_and_auc():
    y = [1, 0, 1, 0]
    assert analysis.average_precision(y, [.9, .8, .7, .1]) == pytest.approx(.5 + .5 * 2 / 3)
    assert analysis.average_precision([1, 0], [.5, .5]) == pytest.approx(.5)  # a tie is one threshold
    assert np.isnan(analysis.average_precision([0, 0], [.1, .2]))
    assert analysis.roc_auc(y, [.9, .8, .7, .1]) == pytest.approx(.75)
    assert analysis.roc_auc([1, 0, 1, 0], [.5, .5, .5, .5]) == pytest.approx(.5)
    assert analysis.roc_auc([1, 1, 0], [.9, .8, .1]) == 1.0


def test_bootstrap_and_mcnemar():
    rng = np.random.default_rng(0)
    y = np.r_[np.ones(40, bool), np.zeros(200, bool)]
    weak = rng.random(240) + .2 * y
    strong = rng.random(240) + 2 * y
    lo, hi = analysis.bootstrap_ap_diff(y, weak, strong, reps=300)
    assert 0 < lo < hi
    assert analysis.bootstrap_ap_diff(y, weak, weak, reps=50) == (0.0, 0.0)

    assert analysis.mcnemar([False] * 10, [True] * 10) == {"only_a": 0, "only_b": 10, "p": round(2 / 1024, 4)}
    assert analysis.mcnemar([True, False], [False, True])["p"] == 1.0
    assert analysis.mcnemar([True, True], [True, True]) == {"only_a": 0, "only_b": 0, "p": 1.0}
    lo, hi = analysis.wilson(5, 10)
    assert lo < .5 < hi


def test_levels_leave_out_clips_that_carry_the_reaction_and_count_dev_everyday():
    groups = {g: [] for g in core.GROUPS}
    groups.update(cheering=[f"c{i}" for i in range(10)], applause=["a0", "a1"], speech=[f"s{i}" for i in range(10)])
    groups[core.EVERYDAY] = [f"e{i}" for i in range(10)]
    labels, dev = {}, {}
    i_app, i_laugh = core.REACTIONS.index("applause"), core.REACTIONS.index("laughter")

    def put(f, lab, **scores):
        labels[f"dev/{f}"] = lab
        dev[f"dev/{f}"] = np.array([scores.get(r, 0.0) for r in core.REACTIONS], np.float32)

    for i in range(10):
        # half the cheering clips are applause too, and score as applause: not false
        put(f"c{i}", ["Cheering", "Applause"] if i < 5 else ["Cheering"], cheering=.5, applause=.6 if i < 5 else 0)
        put(f"s{i}", ["Speech"])
        put(f"e{i}", ["Dog"], laughter=.4 if i < 2 else 0)  # 2 of 10 everyday clips
    put("a0", ["Applause"], applause=.6)
    put("a1", ["Applause"], applause=.6)
    sel = {"dev": {"groups": groups}}
    levels = analysis.choose_levels(dev, sel, labels)
    assert levels["applause"]["chosen"]["found"] == 2 and levels["applause"]["level"] == .6
    assert levels["applause"]["chosen"]["worst_false"] == 0
    # Laughter: no dev laughter clips; levels up to .4 would open 20 % of everyday clips.
    assert levels["laughter"]["level"] == .9 and levels["laughter"]["chosen"]["found"] == 0
    scan = {r["level"]: r for r in levels["laughter"]["scan"]}
    assert scan[.4]["worst_false"] == .2 and scan[.4]["worst_group"] == core.EVERYDAY and not scan[.4]["allowed"]
    # Nothing allowed: never opens.
    for i in range(10):
        dev[f"dev/s{i}"][i_app] = 1.0
    assert analysis.choose_levels(dev, sel, labels)["applause"]["level"] == analysis.NEVER
    assert i_laugh == 0
    assert analysis.opens(np.float32(.9), .9) and not analysis.opens(np.float32(.8994), .9)
    assert analysis.opens(np.array([.9, .8994], np.float32), .9).tolist() == [True, False]


# ---- the whole analysis -------------------------------------------------------------------

def test_analysis_through_serversession(tmp_path):
    import encore_server.detector as detector

    folder = make_data(tmp_path / "data")
    banks = tmp_path / "banks"
    yam = scripted_bank(banks / "yamnet.npz", folder, "yamnet")
    cut = scripted_bank(banks / "cut.npz", folder, "ast-cut1s")
    half_keys = list(cut)[:50]
    core.save_bank(banks / "cut-fp16.npz", half_keys, np.array([cut[k] + .001 for k in half_keys]),
                   {"variant": "ast-cut1s", "precision": "fp16"})
    manifest = json.loads((folder / "manifest.json").read_text())
    clip_keys = [f"{s.split}/{s.fname}" for s in core.streams(manifest["selection"], ["curated", "dev", "everyday"])]
    clip_rows = np.array([[.9 if LABELS[k][0] == "Laughter" else 0, 0, 0, 0, 0, 0] for k in clip_keys], np.float32)
    core.save_bank(banks / "clip.npz", clip_keys, clip_rows,
                   {"variant": "ast-clip", "precision": "fp32", "rows": len(clip_keys), "model_seconds": 1,
                    "per_second": 1, "scorer": {"model": "scripted-clip", "device": "cpu"}})
    timing = []
    for name, device, threads, ms in (("yamnet", "cpu", 2, 4.0), ("yamnet", "cpu", None, 2.0),
                                      ("ast-cut1s", "cuda:0", None, 6.0), ("ast-cut1s", "cpu", 2, 90.0)):
        p = tmp_path / f"timing-{name}-{device}-{threads}.json"
        p.write_text(json.dumps({"variant": name, "device": device, "threads": threads, "precision": "fp32",
                                 "median_ms": ms, "p90_ms": ms, "scorer": {"gpu": "T4" if device != "cpu" else None}}))
        timing.append(p)
    args = argparse.Namespace(data=folder, bank=[f"yamnet={banks / 'yamnet.npz'}", f"ast-cut1s={banks / 'cut.npz'}",
                                                 f"ast-clip={banks / 'clip.npz'}"],
                              check=[f"ast-cut1s={banks / 'cut-fp16.npz'}"], timing=timing, sanity=None,
                              out=tmp_path / "out", workers=1, limit=None, commit="abc1234")
    result = analysis.analyze(args)
    assert detector.REACTIONS == core.PRODUCT and em11.time is time  # nothing leaks out of the replay

    lv = result["levels"]
    assert (lv["yamnet"]["laughter"]["level"], lv["yamnet"]["scream"]["level"], lv["yamnet"]["cheering"]["level"]) == (.5, .3, .4)
    assert lv["ast-cut1s"]["scream"]["chosen"]["found"] == 0
    assert lv["ast-clip"]["laughter"]["level"] == .9  # float32 .9 passes .9 once rounded, as in the detector
    assert lv["yamnet"]["laughter"]["chosen"]["of"] == 2

    s = result["server"]
    assert set(s) == {"yamnet@dev", "yamnet@v3", "ast-cut1s@dev"}
    assert result["latency_ms"] == {"yamnet@dev": 4.0, "yamnet@v3": 4.0, "ast-cut1s@dev": 6.0}
    for cfg in s:
        assert s[cfg]["found"]["laughter"]["k"] == 2 and s[cfg]["false"]["speech"]["total"] == 0
        assert s[cfg]["false"][core.EVERYDAY]["total"] == 0 and s[cfg]["wrong"]["laughter"] == 0
        assert s[cfg]["delay"]["laughter"]["median_s"] > 0
        assert s[cfg]["found_mixed"]["laughter"]["speech+10dB"] == {"k": 2, "n": 2}
    assert s["yamnet@dev"]["found"]["scream"]["k"] == 1 and s["ast-cut1s@dev"]["found"]["scream"]["k"] == 0
    assert s["yamnet@dev"]["found"]["cheering"]["k"] == 1
    # Dev had no applause clips, so applause got the highest level and its .4 opens nothing.
    assert lv["yamnet"]["applause"]["level"] == .9 and s["yamnet@dev"]["opened_on"]["cheering"]["applause"] == 0
    assert s["yamnet@v3"]["found"]["scream"]["k"] == 1  # .3 passes detector-v3's .2 too
    assert result["mcnemar"]["ast-cut1s@dev"]["scream"] == {"only_a": 1, "only_b": 0, "p": 1.0}

    free = result["thresholdless"]
    assert free["yamnet"]["laughter"]["curated"]["ap"] == 1.0 and free["ast-clip"]["laughter"]["curated"]["positives"] == 2
    assert free["ast-cut1s"]["scream"]["curated"]["vs_base"]["diff"] < 0
    assert result["precision_check"]["ast-cut1s"]["windows"] == 50
    assert result["precision_check"]["ast-cut1s"]["flipped"] == 0

    text = (tmp_path / "out" / "report.md").read_text()
    for part in ("## 1. Ranking", "## 2. Levels", "`yamnet@v3`", "## 4. Time per window", "## 5. Half", "abc1234"):
        assert part in text
    rows = json.loads((tmp_path / "out" / "rows.json").read_text())
    assert len(rows) == 3 * len(core.streams(manifest["selection"], ["curated", "mixed", "everyday"]))

    # The same from the command line, with worker processes: the same numbers.
    argv = ["analyze", "--data", str(folder), "--out", str(tmp_path / "out2"), "--workers", "2", "--commit", "abc1234"]
    argv += [a for b in args.bank for a in ("--bank", b)] + [a for c in args.check for a in ("--check", c)]
    argv += [a for t in timing for a in ("--timing", str(t))]
    assert core.main(argv) == 0
    again = json.loads((tmp_path / "out2" / "report.json").read_text())
    assert again["server"] == json.loads(json.dumps(result["server"])) and again["levels"] == json.loads(json.dumps(result["levels"]))


def test_a_preflight_run_goes_through_every_stage_on_a_few_clips(tmp_path, monkeypatch):
    folder = make_data(tmp_path / "data")
    sel = tmp_path / "preflight.json"
    assert core.main(["preflight", "--data", str(folder), "--out", str(sel)]) == 0
    picked = json.loads(sel.read_text())
    assert picked["eval"]["groups"]["laughter"] == ["101"] and len(picked["eval"][core.EVERYDAY]) == 1

    monkeypatch.setattr(models, "make_scorer", lambda *a, **k: FakeScorer())
    assert core.main(["score", "--data", str(folder), "--model", "yamnet", "--out", str(tmp_path / "pre.npz"),
                      "--selection", str(sel)]) == 0
    pre, metas = core.load_bank(tmp_path / "pre.npz")
    assert pre and metas[0]["selection"] == str(sel) and "commit" in metas[0]

    # Every stage reads the same small selection, so its banks hold exactly the windows it replays
    # (the playback paired with a reaction clip depends on the selection).
    banks = tmp_path / "banks"
    scripted_bank(banks / "yamnet.npz", folder, "yamnet", picked)
    scripted_bank(banks / "cut.npz", folder, "ast-cut1s", picked)
    out = tmp_path / "out"
    assert core.main(["analyze", "--data", str(folder), "--selection", str(sel), "--out", str(out), "--workers", "1",
                      "--bank", f"yamnet={banks / 'yamnet.npz'}", "--bank", f"ast-cut1s={banks / 'cut.npz'}"]) == 0
    report = json.loads((out / "report.json").read_text())
    assert report["server"]["yamnet@dev"]["found"]["laughter"]["n"] == 1


def test_the_lookup_model_charges_its_time_and_misses_loudly():
    pcm = noise(.975, seed=3).tobytes()
    model = analysis.LookupModel({core.window_key(pcm): np.arange(6, dtype=np.float32) / 10}, 3.0, "x")
    assert model(pcm) == pytest.approx(dict(zip(core.REACTIONS, [0, .1, .2, .3, .4, .5])))
    assert model.replayed_ms == 3.0
    with pytest.raises(KeyError):
        model(b"\x01\x02" * 100)  # a window the bank lacks stops the analysis


# ---- sanity -------------------------------------------------------------------------------

def test_sanity_compares_every_row_with_the_committed_ones(tmp_path, monkeypatch):
    expected = json.loads(core.SANITY.read_text())

    def fake_run(change=None):
        def main(argv):
            out = Path(argv[argv.index("--json") + 1])
            rows = [{"fname": k.split("|")[0], "condition": k.split("|")[1], "setup": k.split("|")[2], "opened": v}
                    for k, v in expected["opened"].items()]
            rows += [{"fname": f"x{i}", "condition": "clean", "setup": "audio-only", "opened": []}
                     for i in range(expected["info"]["rows"] - len(rows))]
            if change:
                rows[0]["opened"] = change
            out.write_text(json.dumps({"info": {"model": expected["info"]["model"]}, "rows": rows}))
            return 0
        return main

    args = argparse.Namespace(data=tmp_path / "data", work=tmp_path / "work", out=tmp_path / "sanity.json")
    monkeypatch.setattr(analysis.em11, "main", fake_run())
    result = analysis.sanity(args)
    assert result["mismatch_count"] == 0 and result["rows"] == expected["info"]["rows"]
    links = list((tmp_path / "work" / "em11-280" / "16k").iterdir())
    assert len(links) == expected["info"]["clips"] and all(p.is_symlink() for p in links)

    monkeypatch.setattr(analysis.em11, "main", fake_run(change=["gasp"]))
    assert analysis.main(argparse.Namespace(cmd="sanity", **vars(args))) == 0  # a finding, not a failed run
    assert json.loads(args.out.read_text())["mismatch_count"] == 1


# ---- prepare (needs ffmpeg) ---------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")


@needs_ffmpeg
def test_prepare_uses_the_copy_only_when_its_audio_matches(tmp_path, monkeypatch):
    labels = write_labels(tmp_path / "labels", {"eval": EVAL_LABELS, "dev": DEV_LABELS})
    monkeypatch.setattr(core, "MUSIC_SAMPLE", 1)
    monkeypatch.setattr(core, "DEV_EVERYDAY", 1)
    selection_file = tmp_path / "clips.json"
    selection_file.write_text(core.dumps(core.select(labels)))
    monkeypatch.setattr(core, "SELECTION", selection_file)
    copy, mirror = tmp_path / "copy" / "FSD50K.eval_audio", tmp_path / "mirror"
    for i, f in enumerate([*EVAL_LABELS, *DEV_LABELS]):
        pcm = noise(1.2, seed=100 + i, rate=44_100)
        write_wav(copy / f"{f}.wav", pcm, 44_100)
        write_wav(mirror / f"{f}.wav", pcm, 44_100)

    def fake_mirror(split, fname, tmp):
        tmp.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(mirror / f"{fname}.wav", tmp / f"{fname}.wav"))

    monkeypatch.setattr(data, "fetch_mirror", fake_mirror)
    reference = tmp_path / "reference.json"
    monkeypatch.setattr(core, "REFERENCE", reference)

    def run(ref_hash, out):
        reference.write_text(json.dumps({"clips": {"eval/1": {"pcm_sha256": ref_hash, "sha256_16k": "?"}}}))
        return data.prepare(tmp_path / out, source=tmp_path / "copy", labels_dir=labels, crosscheck=2, workers=2,
                            log=lambda *a: None)

    good = run(data.pcm_sha256(copy / "1.wav"), "good")
    c = good["checks"]
    assert c["copy_used"] and c["reference"]["match"] == 1 and c["crosscheck"]["match"] == 2
    assert good["sources"] == {"copy": len(good["files"])} and not good["failed"]
    assert len(good["files"]) == len(core.selected_ids(good["selection"], "eval")) + len(core.selected_ids(good["selection"], "dev"))
    f1 = good["files"]["eval/1"]
    assert f1["samples"] == int(1.2 * RATE) and f1["format"]["rate"] == 44_100
    assert em11.read_pcm(tmp_path / "good" / "16k" / "eval" / "1.wav").size == f1["samples"]
    assert "Copy used: yes" in (tmp_path / "good" / "verify-report.md").read_text()

    bad = run("0" * 64, "bad")
    assert not bad["checks"]["copy_used"] and bad["checks"]["reference"]["mismatch"] == ["eval/1"]
    assert bad["sources"] == {"mirror": len(bad["files"])}
    assert {k: v["sha256_16k"] for k, v in bad["files"].items()} == {k: v["sha256_16k"] for k, v in good["files"].items()}

    selection_file.write_text(core.dumps(core.select(labels) | {"eval": {"groups": {}, core.EVERYDAY: []}}))
    with pytest.raises(RuntimeError):
        run("0" * 64, "changed")
