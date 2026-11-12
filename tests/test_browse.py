import os

import pytest

from app import browse


def test_rejects_dotdot(tmp_path):
    with pytest.raises(browse.UnsafePath):
        browse.resolve(tmp_path, "a/../../etc")


def test_rejects_symlink_traversal(tmp_path):
    (tmp_path / "real").mkdir()
    os.symlink("/", tmp_path / "escape")
    with pytest.raises(browse.UnsafePath):
        browse.resolve(tmp_path, "escape/etc/passwd")
    with pytest.raises(browse.UnsafePath):
        browse.resolve(tmp_path, "escape")
    # final symlink allowed only when explicitly requested (for display)
    assert browse.resolve(tmp_path, "escape", allow_final_symlink=True) == tmp_path / "escape"


def test_resolve_normal(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "hosts").write_text("x")
    assert browse.resolve(tmp_path, "/etc/hosts") == tmp_path / "etc" / "hosts"
    assert browse.resolve(tmp_path, "") == tmp_path
    with pytest.raises(FileNotFoundError):
        browse.resolve(tmp_path, "nope")
