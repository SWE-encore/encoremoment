"""EM-11 model comparison, data stages: ``refhash`` and ``prepare`` (see em11_compare.py).

``prepare`` reads the chosen clips from a copy of FSD50K (on Kaggle, the dataset attached as
input), checks that copy against clips whose content is known, and converts every clip to
16 kHz mono PCM16 with the same ffmpeg arguments em11_eval.py uses. A clip the copy lacks, or
every clip if the copy fails a check, comes from the Hugging Face mirror at a pinned revision.

What is compared is the decoded audio (``pcm_sha256``: ffmpeg's s16le output at the file's own
rate and channels), so a lossless re-encoding of the same audio passes and a resampled or lossy
copy does not.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
import sys
import time
import urllib.request
import wave
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import em11_compare as core
import em11_eval as em11

AUDIO_EXT = {".wav", ".flac", ".ogg", ".mp3", ".m4a", ".aiff", ".aif"}
USER_AGENT = "encore-eval/1"


# ---- one file -----------------------------------------------------------------------------

def tool_version(tool: str = "ffmpeg") -> str:
    out = subprocess.run([tool, "-version"], capture_output=True, text=True, check=True).stdout
    return out.splitlines()[0] if out else tool


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=codec_name,sample_rate,channels,sample_fmt,bits_per_sample", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    stream = (json.loads(out).get("streams") or [{}])[0]
    return {"codec": stream.get("codec_name"), "rate": int(stream.get("sample_rate") or 0),
            "channels": int(stream.get("channels") or 0), "sample_fmt": stream.get("sample_fmt"),
            "bits": int(stream.get("bits_per_sample") or 0)}


def pcm_sha256(path: Path) -> str:
    """SHA-256 of the decoded audio as 16-bit samples, at the file's own rate and channels."""
    proc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
                             "-f", "s16le", "-acodec", "pcm_s16le", "-"], stdout=subprocess.PIPE)
    h = hashlib.sha256()
    for block in iter(lambda: proc.stdout.read(1 << 20), b""):
        h.update(block)
    if proc.wait():
        raise RuntimeError(f"ffmpeg could not decode {path}")
    return h.hexdigest()


def frames(path: Path) -> int:
    with wave.open(str(path)) as w:
        return w.getnframes()


# ---- sources ------------------------------------------------------------------------------

def index_copy(root: Path) -> tuple[dict[str, Path], dict]:
    """Every audio file under ``root`` named by a Freesound id (``12345.wav``), and a summary."""
    found: dict[str, Path] = {}
    dupes = 0
    exts, dirs = Counter(), Counter()
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in AUDIO_EXT or not path.stem.isdigit() or not path.is_file():
            continue
        exts[path.suffix.lower()] += 1
        dirs[str(path.parent.relative_to(root))] += 1
        if path.stem in found:
            dupes += 1
            continue
        found[path.stem] = path
    return found, {"root": str(root), "files": len(found), "duplicates": dupes,
                   "extensions": dict(exts), "folders": dict(dirs.most_common(12))}


def download(url: str, dest: Path, *, retries: int = 5, max_bytes: int | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest.with_suffix(".part"), "wb") as f:
                copied = 0
                for block in iter(lambda: resp.read(1 << 20), b""):
                    copied += len(block)
                    if max_bytes and copied > max_bytes:
                        raise RuntimeError(f"{url}: more than {max_bytes} bytes")
                    f.write(block)
            dest.with_suffix(".part").replace(dest)
            return dest
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def mirror_url(split: str, fname: str) -> str:
    m = core.PINS["mirror"]
    return f"https://huggingface.co/datasets/{m['repo']}/resolve/{m['revision']}/clips/{split}/{fname}.wav"


def fetch_mirror(split: str, fname: str, tmp: Path) -> Path:
    path = download(mirror_url(split, fname), tmp / split / f"{fname}.wav", max_bytes=64_000_000)
    with open(path, "rb") as f:
        head = f.read(12)
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        path.unlink()
        raise RuntimeError(f"mirror {split}/{fname}: not a WAV file")
    return path


def ground_truth(dest: Path) -> Path:
    """FSD50K's labels from Zenodo, checked against the published MD5. Returns the folder with eval.csv."""
    pin = core.PINS["ground_truth"]
    zpath = download(pin["url"], dest / "FSD50K.ground_truth.zip", max_bytes=10_000_000)
    md5 = hashlib.md5(zpath.read_bytes()).hexdigest()
    if md5 != pin["md5"]:
        raise RuntimeError(f"FSD50K.ground_truth.zip: md5 {md5}, expected {pin['md5']}")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest)
    return next(p.parent for p in dest.rglob("eval.csv"))


# ---- refhash ------------------------------------------------------------------------------

def refhash(caches: list[Path]) -> dict:
    """Decoded-audio checksums of the original clips in em11_eval caches, and of their 16 kHz files."""
    clips = {}
    for cache in caches:
        manifest = json.loads((cache / "manifest.json").read_text())
        split = manifest.get("split", "eval")
        for fname in sorted({f for ids in manifest["groups"].values() for f in ids}, key=int):
            raw, small = cache / "clips" / f"{fname}.wav", cache / "16k" / f"{fname}.wav"
            clips[f"{split}/{fname}"] = {"pcm_sha256": pcm_sha256(raw), "format": probe(raw),
                                         "sha256_16k": core.sha256_file(small)}
    return {"what": "EM-11's original clips (docs/em-11-clips.json, docs/em-11-dev-clips.json): checksum of "
                    "the decoded audio of each clip from the Hugging Face mirror, and of its 16 kHz file",
            "ffmpeg": tool_version(), "clips": clips}


# ---- prepare ------------------------------------------------------------------------------

def prepare(out: Path, *, source: Path | None, labels_dir: Path | None, policy: str = "auto",
            crosscheck: int = 30, workers: int = 8, limit: int | None = None, tmp: Path | None = None,
            log=print) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    tmp = tmp or out.parent / "em11-tmp"
    started = time.time()
    if labels_dir is None:
        labels_dir = ground_truth(tmp / "labels")
    selection = core.select(labels_dir)
    committed = json.loads(core.SELECTION.read_text())
    if {s: selection[s]["groups"] for s in ("eval", "dev")} != {s: committed[s]["groups"] for s in ("eval", "dev")} \
            or selection["eval"][core.EVERYDAY] != committed["eval"][core.EVERYDAY]:
        raise RuntimeError("the clips chosen from these labels differ from docs/em-11-compare-clips.json")
    selection = core.limited(selection, limit)
    labels = {split: em11.read_labels(labels_dir / f"{split}.csv") for split in ("eval", "dev")}
    wanted = [(split, f) for split in ("eval", "dev") for f in core.selected_ids(selection, split)]
    log(f"{len(wanted)} clips chosen ({sum(s == 'eval' for s, _ in wanted)} eval, {sum(s == 'dev' for s, _ in wanted)} dev)")

    copy, copy_info = ({}, None) if source is None else index_copy(source)
    if copy_info:
        log(f"copy: {copy_info['files']} audio files, {copy_info['extensions']}")
    checks = {"policy": policy}

    # 1. The copy against clips whose audio is known (EM-11's 695), then against the mirror.
    reference = json.loads(core.REFERENCE.read_text())["clips"]
    known: dict[str, str] = {}

    def check_ref(item):
        key, ref = item
        path = copy[key.split("/")[1]]
        got = pcm_sha256(path)
        known[key] = got
        return key, got == ref["pcm_sha256"], probe(path)

    ref_items = [(k, v) for k, v in reference.items() if k.split("/")[1] in copy] if policy != "mirror" else []
    with ThreadPoolExecutor(workers) as pool:
        ref_results = list(pool.map(check_ref, ref_items))
    checks["reference"] = {"in_copy": len(ref_items), "of": len(reference),
                           "match": sum(ok for _, ok, _ in ref_results),
                           "mismatch": sorted(k for k, ok, _ in ref_results if not ok),
                           "formats": dict(Counter(json.dumps(fmt, sort_keys=True) for _, _, fmt in ref_results))}
    pool_keys = [f"{s}/{f}" for s, f in wanted if f in copy and f"{s}/{f}" not in reference]
    sample = random.Random(core.SEED).sample(pool_keys, min(crosscheck, len(pool_keys))) if policy != "mirror" else []

    def check_mirror(key):
        split, fname = key.split("/")
        try:
            path = fetch_mirror(split, fname, tmp / "crosscheck")
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            return key, None, str(exc)
        try:
            theirs, ours = pcm_sha256(path), pcm_sha256(copy[fname])
            known[key] = ours
            return key, theirs == ours, None
        finally:
            path.unlink(missing_ok=True)

    with ThreadPoolExecutor(min(workers, 4)) as pool:
        cross = list(pool.map(check_mirror, sample))
    checks["crosscheck"] = {"asked": len(sample), "match": sum(ok is True for _, ok, _ in cross),
                            "mismatch": sorted(k for k, ok, _ in cross if ok is False),
                            "errors": {k: e for k, ok, e in cross if ok is None}}
    trusted = (policy == "copy" or (policy == "auto" and checks["reference"]["in_copy"] > 0
               and not checks["reference"]["mismatch"] and not checks["crosscheck"]["mismatch"]
               and (checks["crosscheck"]["match"] > 0 or not sample)))
    checks["copy_used"] = bool(trusted and copy)
    log(f"reference: {checks['reference']['match']}/{checks['reference']['in_copy']} match; "
        f"mirror cross-check: {checks['crosscheck']['match']}/{len(sample)} match; "
        f"copy {'used' if checks['copy_used'] else 'NOT used'}")

    # 2. Every chosen clip: its source, decoded-audio checksum, format, and the 16 kHz file.
    def process(item):
        split, fname = item
        key = f"{split}/{fname}"
        fetched = None
        try:
            if checks["copy_used"] and fname in copy:
                src, origin = copy[fname], "copy"
            else:
                src = fetched = fetch_mirror(split, fname, tmp / "mirror")
                origin = "mirror"
            digest = known.get(key) if origin == "copy" else None
            digest = digest or pcm_sha256(src)
            dst = out / "16k" / split / f"{fname}.wav"
            em11.to_16k(src, dst)
            return key, {"source": origin, "pcm_sha256": digest, "format": probe(src),
                         "sha256_16k": core.sha256_file(dst), "samples": frames(dst)}, None
        except Exception as exc:  # noqa: BLE001 - one bad clip is reported, not fatal
            return key, None, f"{type(exc).__name__}: {exc}"
        finally:
            if fetched is not None:
                fetched.unlink(missing_ok=True)

    files, failed = {}, {}
    done = 0
    with ThreadPoolExecutor(workers) as pool:
        for key, rec, err in pool.map(process, wanted):
            done += 1
            if rec:
                files[key] = rec
            else:
                failed[key] = err
            if done % 500 == 0 or done == len(wanted):
                log(f"{done}/{len(wanted)} clips ({len(failed)} failed, {time.time() - started:.0f} s)")
    for split in ("eval", "dev"):  # a clip that failed leaves the evaluation, and the report says so
        sel = selection[split]
        sel["groups"] = {g: [f for f in ids if f"{split}/{f}" in files] for g, ids in sel["groups"].items()}
        sel[core.EVERYDAY] = [f for f in sel.get(core.EVERYDAY, []) if f"{split}/{f}" in files]
    sixteen = {k: v["sha256_16k"] for k, v in files.items() if k in reference}
    checks["16k_vs_reference"] = {"compared": len(sixteen),
                                  "identical": sum(reference[k]["sha256_16k"] == v for k, v in sixteen.items())}
    manifest = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pins": core.PINS, "ffmpeg": tool_version(), "limit": limit, "copy": copy_info, "checks": checks,
        "sources": dict(Counter(v["source"] for v in files.values())), "failed": failed,
        "formats": dict(Counter(json.dumps(v["format"], sort_keys=True) for v in files.values())),
        "selection": selection,
        "labels": {f"{s}/{f}": sorted(labels[s][f]) for s, f in wanted if f"{s}/{f}" in files},
        "files": files,
        "seconds": round(time.time() - started),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    (out / "verify-report.md").write_text(verify_report(manifest))
    shutil.rmtree(tmp / "mirror", ignore_errors=True)
    shutil.rmtree(tmp / "crosscheck", ignore_errors=True)
    return manifest


def verify_report(m: dict) -> str:
    c = m["checks"]
    ref, cross = c["reference"], c["crosscheck"]
    hours = sum(v["samples"] for k, v in m["files"].items() if k.startswith("eval/")) / em11.RATE / 3600
    lines = [
        "# EM-11 comparison data: what was checked\n",
        f"- Created {m['created']} with {m['ffmpeg']}; policy `{c['policy']}`" + (f"; smoke run, limit {m['limit']}" if m["limit"] else ""),
        f"- Copy: {json.dumps(m['copy']) if m['copy'] else 'none given'}",
        f"- Reference clips (EM-11's 695, from the mirror) found in the copy: {ref['in_copy']} of {ref['of']}; "
        f"decoded audio identical: {ref['match']}; different: {len(ref['mismatch'])} {ref['mismatch'][:10]}",
        f"- Formats of those copy files: {ref['formats']}",
        f"- Mirror cross-check of other copy clips: {cross['match']} of {cross['asked']} identical; "
        f"different: {cross['mismatch'][:10]}; errors: {len(cross['errors'])}",
        f"- **Copy used: {'yes' if c['copy_used'] else 'no'}.** Clips by source: {m['sources']}",
        f"- 16 kHz files identical to EM-11's own: {c['16k_vs_reference']['identical']} of {c['16k_vs_reference']['compared']}",
        f"- Failed clips (left out): {len(m['failed'])} {list(m['failed'].items())[:5]}",
        f"- Source formats: {m['formats']}",
        f"- Eval audio: {hours:.1f} h; took {m['seconds']} s",
        "",
        "| Split | Group | Clips |", "|---|---|---|",
    ]
    for split in ("eval", "dev"):
        sel = m["selection"][split]
        for g, ids in sel["groups"].items():
            lines.append(f"| {split} | {g} | {len(ids)} |")
        if sel.get(core.EVERYDAY):
            lines.append(f"| {split} | everyday | {len(sel[core.EVERYDAY])} |")
    return "\n".join(lines) + "\n"


def main(args) -> int:
    if args.cmd == "refhash":
        result = refhash([c.expanduser() for c in args.cache])
        rows = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v, sort_keys=True)}" for k, v in result["clips"].items())
        head = json.dumps({k: v for k, v in result.items() if k != "clips"}, indent=1)[:-2]
        args.out.write_text(f'{head},\n "clips": {{\n{rows}\n }}\n}}\n')
        print(f"{len(result['clips'])} clips -> {args.out}")
        return 0
    manifest = prepare(args.out, source=args.source, labels_dir=args.labels.expanduser() if args.labels else None,
                       policy=args.policy, crosscheck=args.crosscheck, workers=args.workers, limit=args.limit,
                       tmp=args.tmp)
    print(verify_report(manifest))
    return 1 if not manifest["files"] else 0


if __name__ == "__main__":
    sys.exit(core.main(["prepare", *sys.argv[1:]]))
