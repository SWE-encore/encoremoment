"""Shared pytest setup for contracts/, client/ and server/ tests (pytest.ini)."""

import importlib.util
import os

# When TensorFlow is installed (server/requirements-yamnet.txt), load it before any test starts
# Qt Multimedia or GStreamer. Those can pull in the system's libLLVM through the graphics
# drivers, and TensorFlow loaded after that crashes the process inside its own LLVM code.
# The test bench does the same (client/encore_client/bench/local.py, preload_tensorflow).
if importlib.util.find_spec("tensorflow") is not None:
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    import tensorflow  # noqa: F401
