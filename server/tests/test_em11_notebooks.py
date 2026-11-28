"""The EM-11 Kaggle notebooks (server/tools/kaggle/*.ipynb) without Kaggle, GPUs or models.

A full notebook 2 run takes hours, so a mistake in its own code (a wrong flag, shard or file name,
or resume that reuses the wrong files) must show here, not on Kaggle. Notebook 2 runs cell by cell:
each command it gives the tool goes through the tool's real argument parser and writes a stand-in
output; git, pip, GPUs and models are never touched. Kaggle keeps no output of a version that ends
in an error, so after the preflight a failed stage must not fail the notebook.
"""
import ast
import builtins
import io
import subprocess
import json
import sys
import tarfile
import time
import types
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
KAGGLE = ROOT / "server/tools/kaggle"
TOOLS = ROOT / "server/tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import em11_compare as core  # noqa: E402

CREATED = "2026-11-27T10:00:00+00:00"


def code_cells(name):
    book = json.loads((KAGGLE / name).read_text())
    return ["".join(c["source"]) for c in book["cells"] if c["cell_type"] == "code"]


@pytest.mark.parametrize("name", ["em11-prepare.ipynb", "em11-compare.ipynb"])
def test_notebook_code_parses_and_defines_every_name_it_reads(name):
    tree = ast.parse("\n\n".join(code_cells(name)))
    defined = set(dir(builtins)) | {"display"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
    unknown = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} - defined
    assert not unknown


@pytest.mark.parametrize("name", ["em11-prepare.ipynb", "em11-compare.ipynb"])
def test_notebooks_run_the_main_branch(name):
    # A feature branch is deleted once merged; Kaggle then cannot check it out.
    assert 'REF = "main"' in code_cells(name)[0]


def bank(path, variant, precision="fp32", created=CREATED, **meta):
    meta = {"variant": variant, "precision": precision, "limit": None, "rows": 3, "per_second": 1.0,
            "wall_seconds": 3, "data_created": created, **meta}
    np.savez(path, keys=np.array(["k1", "k2", "k3"]), scores=np.zeros((3, 6), np.float32), meta=json.dumps(meta))


class Stand:
    """Stand-ins for the commands notebook 2 runs, recording what the tool was asked to do. A command
    whose output file is named in ``fail`` fails, as a crashing stage would."""

    def __init__(self, monkeypatch, fail=()):
        self.ran, self.fail = [], set(fail)
        fake = types.ModuleType("fake")
        fake.main = self._handle
        for module in ("em11_compare_models", "em11_compare_analysis"):
            monkeypatch.setitem(sys.modules, module, fake)
        monkeypatch.setattr(core, "preflight", lambda m, n: ({"eval": {"groups": {}}, "dev": {"groups": {}}}, {}))

    def tool(self, argv):
        """Run one tool command; False if it fails."""
        argv = [str(a) for a in argv]
        if "--out" in argv and Path(argv[argv.index("--out") + 1]).stem in self.fail:
            self.ran.append(("failed", Path(argv[argv.index("--out") + 1]).stem))
            return False
        assert core.main(argv) == 0
        if argv[0] == "preflight":
            self.ran.append(("preflight",))
        return True

    def _handle(self, a):
        if a.cmd == "score":
            i, n = map(int, a.shard.split("/"))
            assert 0 <= i < n, f"{a.out.name}: shard {a.shard}"
            assert a.device == "cpu" or int(a.device.split(":")[1]) < 2, a.device
            for f in a.skip:
                assert f.exists(), f"{a.out.name}: --skip {f.name} is not scored yet"
            bank(a.out, a.model, a.precision)
            self.ran.append(("score", a.out.name, bool(a.selection)))
        elif a.cmd == "timing":
            a.out.write_text("{}")
            self.ran.append(("timing", a.out.name))
        elif a.cmd == "analyze":
            for spec in a.bank + a.check:
                for f in spec.split("=", 1)[1].split(","):
                    assert Path(f).exists(), f"analyze reads {f}, which no stage wrote"
            a.out.mkdir(parents=True, exist_ok=True)
            (a.out / "report.md").write_text("# report\n")
            self.ran.append(("analyze", len(a.bank), bool(a.sanity), bool(a.selection)))
        elif a.cmd == "sanity":
            a.out.write_text("{}")
            self.ran.append(("sanity",))
        return 0

    def sh(self, *cmd, check=True, cwd=None, tail=None):
        cmd = [str(c) for c in cmd]
        if cmd[0] == "tar":
            with tarfile.open(cmd[2]) as t:
                t.extractall(cmd[4], filter="data")
        elif len(cmd) > 1 and cmd[1].endswith("em11_compare.py") and not self.tool(cmd[2:]) and check:
            raise subprocess.CalledProcessError(1, cmd)
        return 0

    @staticmethod
    def out(*cmd, cwd=None):
        text = " ".join(map(str, cmd))
        if "rev-parse" in text:
            return "f" * 40
        if "device_count" in text:
            return "2"
        if "PINS" in text:
            return json.dumps(core.PINS)
        if str(cmd[0]) == "sha256sum":
            return core.PINS["ast"]["sha256"] + "  file"
        raise AssertionError(f"unexpected command: {text}")

    def popen(self, cmd, stdout=None, stderr=None, cwd=None):
        code = 0 if self.tool(cmd[2:]) else 1
        stdout.write("done\n" if not code else "RuntimeError: stand-in failure\n")
        stdout.flush()
        return types.SimpleNamespace(returncode=code, poll=lambda: code, terminate=lambda: None)


FINISHED = ["yamnet", "pad10s-fp32-0", "pad10s-fp32-1", "pad10s-fp16-0", "pad10s-fp16-1", "cut1s-0", "cut1s-1"]


def run_notebook(tmp_path, monkeypatch, fail=(), earlier=True):
    """Run em11-compare.ipynb cell by cell with stand-ins; with ``earlier``, the output of a run in which
    every stage but ast-clip finished is attached. Returns the notebook's names, the stand-ins and the
    Kaggle working folder."""
    places = {p: tmp_path / Path(p).name for p in ("/kaggle/working", "/kaggle/input", "/tmp/encoremoment",
                                                   "/tmp/em11-data", "/tmp/ast", "/tmp/preflight", "/tmp/sanity")}
    for p in places.values():
        p.mkdir()
    manifest = {"limit": None, "files": {"x": 1}, "created": CREATED, "checks": {"copy_used": True}, "selection": {}}
    prepared = tmp_path / "input/notebooks/me/prepare"
    prepared.mkdir(parents=True)
    with tarfile.open(prepared / "em11-data.tar", "w") as t:
        for name, text in (("manifest.json", json.dumps(manifest)), ("verify-report.md", "ok\n"),
                           ("code.json", json.dumps({"repo": "https://example.invalid/repo.git", "commit": "c"}))):
            info = tarfile.TarInfo(f"em11-data/{name}")
            info.size = len(text.encode())
            t.addfile(info, io.BytesIO(text.encode()))
    if earlier:
        folder = tmp_path / "input/notebooks/me/compare/banks"
        folder.mkdir(parents=True)
        for name in FINISHED:
            bank(folder / f"{name}.npz", "ast")
        bank(folder / "clip-0.npz", "ast-clip", created="another prepare run")
        bank(folder / "clip-1.npz", "ast-clip", selection="/tmp/preflight/selection.json")

    stand = Stand(monkeypatch, fail)
    for var in ("TF_CPP_MIN_LOG_LEVEL", "TFHUB_CACHE_DIR"):  # the notebook sets these; put them back after
        monkeypatch.setenv(var, "")
    monkeypatch.setitem(sys.modules, "IPython.display", types.SimpleNamespace(Markdown=str, display=lambda *a: None))
    ns = {"__name__": "__main__"}
    for i, code in enumerate(code_cells("em11-compare.ipynb")):
        for a, b in places.items():
            code = code.replace(a, str(b))
        exec(compile(code, f"cell {i}", "exec"), ns)
        if "def parallel" in code:
            ns.update(sh=stand.sh, out=stand.out, venv=lambda name, requirements: f"/venv-{name}/bin/python",
                      subprocess=types.SimpleNamespace(Popen=stand.popen, STDOUT=-2),
                      time=types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    return ns, stand, places["/kaggle/working"]


def test_compare_notebook_preflights_then_reuses_an_earlier_runs_scores(tmp_path, monkeypatch, capsys):
    ns, stand, work = run_notebook(tmp_path, monkeypatch)

    assert sorted(ns["REUSED"]) == sorted(f"{n}.npz" for n in FINISHED)
    log = capsys.readouterr().out
    assert log.index("PREFLIGHT PASSED") < log.index("started clip-0")
    assert all(f"{n}: kept from an earlier run" in log for n in FINISHED)

    order = [r[0] for r in stand.ran]
    preflight = stand.ran[:order.index("analyze") + 1]
    assert preflight[0] == ("preflight",)
    assert {r[1] for r in preflight if r[0] == "score"} == {
        "yamnet.npz", "pad10s-fp32.npz", "pad10s-fp16.npz", "cut1s.npz", "clip.npz", "pad10s-fp16-sample.npz"}
    assert all(r[2] for r in preflight if r[0] == "score")
    assert preflight[-1] == ("analyze", 4, False, True)
    main = stand.ran[len(preflight):]
    assert [r[1] for r in main if r[0] == "score"] == ["clip-0.npz", "clip-1.npz", "pad10s-fp16-sample.npz"]
    assert ("sanity",) in main and main[-1] == ("analyze", 4, True, False)
    assert (work / "results/report.md").exists()
    assert json.loads((work / "results/run.json").read_text())["complete"] is True
    assert "RUN COMPLETE" in log and "RUN INCOMPLETE" not in log


@pytest.mark.parametrize("fail, earlier, failed, scored", [
    # whole clips fail on one GPU: the other GPU's share, the fp16 sample and the timings still run
    (["clip-1", "cut1s-t4"], True, {"clip-1": "exit 1", "timing cut1s-t4": "CalledProcessError",
                                    "analyze": "not run: needs clip-1.npz"},
     ["clip-0.npz", "pad10s-fp16-sample.npz"]),
    # a fresh run whose full-precision share fails: the half-precision stage that skips its windows waits
    (["pad10s-fp32-0"], False, {"pad10s-fp32-0": "exit 1", "pad10s-fp16-0": "not run: needs pad10s-fp32-0.npz",
                                "pad10s-fp16-1": "not run: needs pad10s-fp32-0.npz", "analyze": "not run: needs"},
     ["clip-0.npz", "clip-1.npz", "cut1s-0.npz", "cut1s-1.npz", "yamnet.npz", "pad10s-fp32-1.npz",
      "pad10s-fp16-sample.npz"]),
])
def test_a_stage_that_fails_after_the_preflight_still_lets_the_notebook_finish(
        tmp_path, monkeypatch, capsys, fail, earlier, failed, scored):
    ns, stand, work = run_notebook(tmp_path, monkeypatch, fail=fail, earlier=earlier)  # no exception: Kaggle keeps the output

    assert set(ns["FAILED"]) == set(failed)
    assert all(ns["FAILED"][name].startswith(why) for name, why in failed.items())
    after = stand.ran[[r[0] for r in stand.ran].index("analyze") + 1:]  # the preflight passed
    assert [r[1] for r in after if r[0] == "score"] == scored
    assert not any(r[0] == "analyze" for r in after)
    assert sorted(f.name for f in (work / "banks").glob("*.npz")) == sorted(
        scored + ([f"{n}.npz" for n in FINISHED] if earlier else []))
    run = json.loads((work / "results/run.json").read_text())
    assert run["complete"] is False and set(run["failed"]) == set(failed)
    assert "RUN INCOMPLETE" in capsys.readouterr().out
    assert (work / "results/verify-report.md").exists()


def test_a_failure_in_the_preflight_stops_the_notebook_before_any_long_stage(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="preflight-clip failed"):
        run_notebook(tmp_path, monkeypatch, fail=["clip"])
