"""The bench's built-in session server, model loading, and audio file preparation."""

from __future__ import annotations

import importlib.util
import os
import queue
import shutil
import subprocess
import sys
import threading
import wave
from pathlib import Path
from typing import Any

from ..session.contract import AUDIO_RATE

SERVER_DIR = Path(__file__).resolve().parents[3] / "server"
MODELS = {"loudness": "Loudness stand-in", "yamnet": "YAMNet"}
YAMNET_HINT = "YAMNet needs TensorFlow: .venv/bin/python -m pip install -r server/requirements-yamnet.txt"


def import_server():
    """The ``encore_server`` package from this checkout's server/ directory."""
    try:
        import encore_server
    except ImportError:
        if not (SERVER_DIR / "encore_server").is_dir():
            raise ImportError(f"{SERVER_DIR} not found: run the bench from a repository checkout") from None
        sys.path.insert(0, str(SERVER_DIR))
        import encore_server
    return encore_server


def preload_tensorflow() -> None:
    """Import TensorFlow, if it is installed, before Qt Multimedia opens audio devices.

    Listing the devices can load the system's libLLVM (through the graphics
    drivers). If TensorFlow is loaded after that, its own LLVM code binds to the
    system copy and YAMNet crashes the process. Loaded first, it keeps its own.
    """
    if importlib.util.find_spec("tensorflow") is None:
        return
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    import tensorflow  # noqa: F401


def default_triggers() -> dict[str, float]:
    import_server()
    from encore_server.detector import DetectorConfig

    return dict(DetectorConfig().trigger)


class Models:
    """Loads models on a background thread; YAMNet takes several seconds the first time."""

    def __init__(self) -> None:
        self._ready: dict[str, Any] = {}
        self._errors: dict[str, str] = {}
        self._loading: set[str] = set()
        self._lock = threading.Lock()

    def request(self, name: str) -> None:
        with self._lock:
            if name in self._ready or name in self._errors or name in self._loading:
                return
            self._loading.add(name)
        threading.Thread(target=self._load, args=(name,), name=f"load-{name}", daemon=True).start()

    def state(self, name: str) -> tuple[str, Any]:
        """("ready", model), ("loading", None) or ("error", message)."""
        with self._lock:
            if name in self._ready:
                return "ready", self._ready[name]
            if name in self._errors:
                return "error", self._errors[name]
        return "loading", None

    def _load(self, name: str) -> None:
        try:
            import_server()
            from encore_server.models import load_model

            model, error = load_model(name), None
        except ImportError as exc:
            model, error = None, (YAMNET_HINT if name == "yamnet" else str(exc))
        except Exception as exc:  # a download or TensorFlow failure: show it, keep the bench usable
            model, error = None, f"{MODELS.get(name, name)} failed to load: {exc}"
        with self._lock:
            self._loading.discard(name)
            if model is not None:
                self._ready[name] = model
            else:
                self._errors[name] = error


class LocalServer:
    """A session server on a free local port, run on a background thread."""

    def __init__(self, model, triggers: dict[str, float]) -> None:
        import_server()
        from encore_server.detector import DetectorConfig
        from encore_server.server import EncoreServer, serve_in_thread

        default = DetectorConfig()
        name = default.name
        if triggers != default.trigger:  # results from other thresholds must not carry the default name
            name = "bench " + " ".join(f"{k}={v:.2f}" for k, v in sorted(triggers.items()))
        self.config = DetectorConfig(name=name, trigger=dict(triggers))
        self.model_name = model.name
        self._frames: queue.SimpleQueue = queue.SimpleQueue()
        self.app = EncoreServer(model, self.config, on_frame=lambda sid, f: self._frames.put((sid, f)))
        self.url, self._stop = serve_in_thread(self.app)

    def frames(self, session_id: str) -> list:
        """Frames analysed since the last call, for one session."""
        out = []
        while True:
            try:
                sid, frame = self._frames.get_nowait()
            except queue.Empty:
                return out
            if sid == session_id:
                out.append(frame)

    def session(self, session_id: str):
        return self.app.sessions.get(session_id)

    def stop(self) -> None:
        self._stop()


def prepare_audio(path: Path, workdir: Path) -> Path:
    """The file in the contract's format: as it is if it already is, else converted with ffmpeg.

    Anything ffmpeg reads works (WAV, MP3, a video's soundtrack...).
    """
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getcomptype()) == (AUDIO_RATE, 1, 2, "NONE"):
                return path
    except (wave.Error, EOFError):
        pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ValueError(f"{path.name} is not 16 kHz mono 16-bit WAV, and ffmpeg is not installed to convert it")
    out = Path(workdir) / f"{path.stem}-16k.wav"
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(path.resolve()),
         "-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-sample_fmt", "s16", str(out)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0 or not out.exists():
        raise ValueError(f"ffmpeg could not read {path.name}: {result.stderr.strip()[-300:]}")
    return out
