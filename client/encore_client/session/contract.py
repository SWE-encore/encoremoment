"""The shared encore.v1 receiver rules, loaded from contracts/ (EM-1).

The client does not keep its own copy of the dedup/order rules. It loads
``contracts/reference/encore_contract.py``, the same code the contract samples
are tested against, so a change to the contract changes the client too.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts"
SAMPLES_DIR = CONTRACTS_DIR / "samples"
_REFERENCE = CONTRACTS_DIR / "reference" / "encore_contract.py"
_NAME = "encore_contract"


def _load():
    if _NAME in sys.modules:
        return sys.modules[_NAME]
    if not _REFERENCE.exists():
        raise ImportError(
            f"{_REFERENCE} not found. Run the client from a repository checkout that "
            "contains contracts/ (EM-1)."
        )
    spec = importlib.util.spec_from_file_location(_NAME, _REFERENCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


_contract = _load()

SCHEMA = "encore.v1"
SessionView = _contract.SessionView
PC_TO_SERVER = _contract.PC_TO_SERVER
SERVER_TO_PC = _contract.SERVER_TO_PC
AUDIO_RATE = _contract.AUDIO_RATE
AUDIO_MAX_SAMPLES = _contract.AUDIO_MAX_SAMPLES
encode_audio_frame = _contract.encode_audio_frame
decode_audio_frame = _contract.decode_audio_frame
