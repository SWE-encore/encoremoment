"""Container startup gates and orderly process termination (no docker daemon required)."""
import asyncio
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[2]


class DeploymentTests(unittest.TestCase):
    def test_pinned_bundle_is_checked_before_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            weights = {"models/tfhub/v1/saved_model.pb": b"fixture", "models/face_landmarker.task": b"face"}
            lines = []
            for name, content in weights.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                lines.append(f"{hashlib.sha256(content).hexdigest()}  {name}\n")
            manifest = root / "reports/model-sha256.txt"
            manifest.parent.mkdir()
            manifest.write_text("".join(lines))
            python = root / "python"
            python.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            python.chmod(0o755)
            env = os.environ | {"PATH": f"{root}:{os.environ['PATH']}", "ENCORE_MODEL": "yamnet",
                                "ENCORE_HISTORY": "1", "ENCORE_REQUIRE_GPU": "0", "ENCORE_BUNDLE_DIR": str(root),
                                "ENCORE_MANIFEST_SHA256": hashlib.sha256(manifest.read_bytes()).hexdigest()}

            def start():
                return subprocess.run(["sh", str(ROOT / "deploy/start-server.sh")], env=env,
                                      capture_output=True, text=True, timeout=5)

            good = start()
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertIn(str(root / "models/tfhub/v1"), good.stdout.splitlines())
            self.assertIn("--record-history", good.stdout.splitlines())
            env["ENCORE_MANIFEST_SHA256"] = "0" * 64
            self.assertNotEqual(start().returncode, 0)
            env["ENCORE_MANIFEST_SHA256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
            (root / "models/tfhub/v1/saved_model.pb").write_bytes(b"changed")
            self.assertNotEqual(start().returncode, 0)

    @unittest.skipIf(sys.platform == "win32", "POSIX container termination")
    def test_sigterm_allows_async_drain(self):
        program = """
import asyncio
from types import SimpleNamespace
from encore_server.__main__ import _serve
class App:
    async def run(self, *args, **kwargs):
        print("ready", flush=True)
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(.01)
            print("drained", flush=True)
asyncio.run(_serve(App(), SimpleNamespace(host="127.0.0.1", port=0, unix_socket=None)))
"""
        with subprocess.Popen([sys.executable, "-c", program], text=True, stdout=subprocess.PIPE,
                              env=os.environ | {"PYTHONPATH": str(ROOT / "server")}) as proc:
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                proc.send_signal(signal.SIGTERM)
                out, _ = proc.communicate(timeout=5)
                self.assertEqual((proc.returncode, out.strip()), (0, "drained"))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_readiness_includes_enabled_dependencies(self):
        from encore_server.history import HistoryUnavailable
        from encore_server.server import EncoreServer

        history = SimpleNamespace(check=AsyncMock())
        face = SimpleNamespace(ready=True)
        app = EncoreServer(Mock(), face_model=face, history=history)
        request = SimpleNamespace(path="/readyz")
        conn = SimpleNamespace(respond=lambda status, message: status)
        self.assertEqual(asyncio.run(app.check_path(conn, request)), HTTPStatus.OK)
        history.check.side_effect = HistoryUnavailable()
        self.assertEqual(asyncio.run(app.check_path(conn, request)), HTTPStatus.SERVICE_UNAVAILABLE)
        live = SimpleNamespace(path="/livez")
        self.assertEqual(asyncio.run(app.check_path(conn, live)), HTTPStatus.OK)
        history.check.side_effect = None
        face.ready = False
        self.assertEqual(asyncio.run(app.check_path(conn, request)), HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(asyncio.run(app.check_path(conn, live)), HTTPStatus.SERVICE_UNAVAILABLE)
