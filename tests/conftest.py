import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = Path(tempfile.mkdtemp(prefix="backupkrake-test-"))
_shim = Path(__file__).parent / "fakessh"
_shim.chmod(_shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

os.environ.update({
    "BACKUPKRAKE_DATA_DIR": str(_tmp / "data"),
    "BACKUPKRAKE_CONFIG_DIR": str(_tmp / "config"),
    "BACKUPKRAKE_SSH_BIN": str(_shim),
    "BACKUPKRAKE_SCHEDULER": "0",
    "BACKUPKRAKE_ADMIN_PASSWORD": "test-pw",
})
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app import db, sshkeys  # noqa: E402

db.init()
sshkeys.ensure_key()


@pytest.fixture
def tmp_root():
    return _tmp


@pytest.fixture(autouse=True)
def _db_open():
    """TestClient shuts the app down (closing the DB) on exit; reopen for later tests."""
    if db._conn is None:
        db.init()
    yield
