"""One place decides what each OS supports; the app and its tests follow it (PR #39, #40)."""

import ast
from pathlib import Path

import pytest

from encore_client.platform_support import (
    camera_unavailable_reason, desktop_capture, native_microphone_format, os_label,
)

PACKAGE = Path(__file__).resolve().parents[1] / "encore_client"


@pytest.mark.parametrize("platform, capture, native, label", [
    ("linux", True, False, "Linux"),
    ("darwin", False, True, "macOS"),
    ("win32", False, False, "Windows"),
    ("freebsd14", False, False, "freebsd14"),
])
def test_each_os_gets_its_capabilities(platform, capture, native, label):
    assert desktop_capture(platform) is capture
    assert native_microphone_format(platform) is native
    assert os_label(platform) == label


def test_the_camera_reason_names_the_os_only_where_there_is_no_recorder():
    assert camera_unavailable_reason("linux") is None
    assert camera_unavailable_reason("darwin") == "not supported on macOS yet; the recorder needs Linux V4L2"
    assert "Windows" in camera_unavailable_reason("win32")


def test_without_an_argument_the_running_os_is_used(monkeypatch):
    import encore_client.platform_support as support
    monkeypatch.setattr(support.sys, "platform", "darwin")
    assert native_microphone_format() and not desktop_capture()


def test_no_other_module_reads_sys_platform():
    """New platform checks belong in platform_support, so the README table stays true."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "platform_support.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and node.attr == "platform"
                    and isinstance(node.value, ast.Name) and node.value.id == "sys"):
                offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert not offenders, f"use encore_client.platform_support instead: {offenders}"
