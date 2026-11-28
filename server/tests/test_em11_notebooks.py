"""The EM-11 Kaggle notebooks (server/tools/kaggle/*.ipynb) without Kaggle, GPUs or models.

A full notebook 2 run takes hours, so a mistake in its own code (a wrong flag, shard or file name,
or resume that reuses the wrong files) must show here, not on Kaggle. Notebook 2 runs cell by cell:
each command it gives the tool goes through the tool's real argument parser and writes a stand-in
output; git, pip, GPUs and models are never touched.
"""
import ast
import builtins
import io
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
    """Stand-ins for the commands notebook 2 runs, recording what the tool was asked to do."""

    def __init__(self, monkeypatch):
        self.ran = []
        fake = types.ModuleType("fake")
        fake.main = self._handle
        for module in ("em11_compare_models", "em11_compare_analysis"):
            monkeypatch.setitem(sys.modules, module, fake)
        monkeypatch.setattr(core, "preflight", lambda m, n: ({"eval": {"groups": {}}, "dev": {"groups": {}}}, {}))

    def tool(self, argv):
        argv = [str(a) for a in argv]
        assert core.main(argv) == 0
        if argv[0] == "preflight":
            self.ran.append(("preflight",))

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
        elif len(cmd) > 1 and cmd[1].endswith("em11_compare.py"):
            self.tool(cmd[2:])
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
        self.tool(cmd[2:])
        stdout.write("done\n")
        stdout.flush()
        return types.SimpleNamespace(returncode=0, poll=lambda: 0, terminate=lambda: None)


def test_compare_notebook_preflights_then_reuses_an_earlier_runs_scores(tmp_path, monkeypatch, capsys):
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
    earlier = tmp_path / "input/notebooks/me/compare/banks"  # the failed run: every stage but ast-clip finished
    earlier.mkdir(parents=True)
    finished = ["yamnet", "pad10s-fp32-0", "pad10s-fp32-1", "pad10s-fp16-0", "pad10s-fp16-1", "cut1s-0", "cut1s-1"]
    for name in finished:
        bank(earlier / f"{name}.npz", "ast")
    bank(earlier / "clip-0.npz", "ast-clip", created="another prepare run")
    bank(earlier / "clip-1.npz", "ast-clip", selection="/tmp/preflight/selection.json")

    stand = Stand(monkeypatch)
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

    assert sorted(ns["REUSED"]) == sorted(f"{n}.npz" for n in finished)
    log = capsys.readouterr().out
    assert log.index("PREFLIGHT PASSED") < log.index("started clip-0")
    assert all(f"{n}: kept from an earlier run" in log for n in finished)

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
    assert (places["/kaggle/working"] / "results/report.md").exists()
