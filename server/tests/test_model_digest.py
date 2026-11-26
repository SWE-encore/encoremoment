"""Model weights are named by their SHA-256, so every event shows which weights produced it (R2)."""

import hashlib
import shutil
import subprocess

import pytest

from encore_server.models import SHORT_DIGEST, sha256_file, sha256_tree, tagged


def model_dir(tmp_path):
    (tmp_path / "variables").mkdir(parents=True)
    (tmp_path / "saved_model.pb").write_bytes(b"graph")
    (tmp_path / "variables" / "variables.index").write_bytes(b"index")
    (tmp_path / "Assets.csv").write_bytes(b"upper case sorts before lower case in C order")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "yamnet_class_map.csv").write_bytes(b"index,mid,display_name\n")
    return tmp_path


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs coreutils sha256sum")
def test_the_tree_digest_is_what_sha256sum_gives_for_the_directory(tmp_path):
    root = model_dir(tmp_path)
    shell = subprocess.run("find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum",
                           shell=True, cwd=root, capture_output=True, text=True, check=True)
    assert sha256_tree(root) == shell.stdout.split()[0]


def test_any_changed_byte_changes_the_digest_and_an_empty_directory_is_refused(tmp_path):
    root = model_dir(tmp_path / "m")
    before = sha256_tree(root)
    (root / "variables" / "variables.index").write_bytes(b"indeX")
    assert sha256_tree(root) != before
    assert sha256_file(root / "saved_model.pb") == hashlib.sha256(b"graph").hexdigest()
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        sha256_tree(tmp_path / "empty")


def test_a_tagged_name_keeps_a_short_digest():
    digest = "0123456789abcdef" * 4
    assert tagged("google/yamnet/1", digest) == "google/yamnet/1@sha256:" + digest[:SHORT_DIGEST]
