"""The shared encore.v1 rules and schema, loaded from contracts/ (EM-1).

Like the PC client, the server does not keep its own copy of the contract.
It loads the reference module and the JSON schema from the repository
checkout, so both ends follow the same rules.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
_REFERENCE = CONTRACTS_DIR / "reference" / "encore_contract.py"
_NAME = "encore_contract"


def _load():
    if _NAME in sys.modules:
        return sys.modules[_NAME]
    if not _REFERENCE.exists():
        raise ImportError(f"{_REFERENCE} not found. Run the server from a repository checkout.")
    spec = importlib.util.spec_from_file_location(_NAME, _REFERENCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


_contract = _load()

SessionView = _contract.SessionView
PC_TO_SERVER = _contract.PC_TO_SERVER
AUDIO_RATE = _contract.AUDIO_RATE
decode_audio_frame = _contract.decode_audio_frame

SCHEMA = json.loads((CONTRACTS_DIR / "schema" / "messages.schema.json").read_text())
_VALIDATOR = Draft202012Validator(SCHEMA)


def schema_errors(msg) -> list[str]:
    return [e.message for e in _VALIDATOR.iter_errors(msg)]
