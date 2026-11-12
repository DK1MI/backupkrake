"""Runtime configuration, read from environment variables."""
import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DATA_DIR = Path(_env("BACKUPKRAKE_DATA_DIR", "/data"))
CONFIG_DIR = Path(_env("BACKUPKRAKE_CONFIG_DIR", "/config"))

HOSTS_DIR = DATA_DIR / "hosts"
RESTORE_DIR = DATA_DIR / "restore"
LOG_DIR = CONFIG_DIR / "logs"
RESTORE_STAGING = "/var/tmp/backupkrake-restore"  # on the client
SSH_DIR = CONFIG_DIR / "ssh"
SSH_KEY = SSH_DIR / "id_ed25519"
RESTORE_KEY = SSH_DIR / "id_ed25519_restore"
KNOWN_HOSTS = SSH_DIR / "known_hosts"
DB_PATH = CONFIG_DIR / "backupkrake.db"
SECRET_FILE = CONFIG_DIR / "secret_key"
INITIAL_PASSWORD_FILE = CONFIG_DIR / "initial_admin_password"
RESTIC_CACHE = CONFIG_DIR / "restic-cache"

SSH_BIN = _env("BACKUPKRAKE_SSH_BIN", "ssh")
RSYNC_BIN = _env("BACKUPKRAKE_RSYNC_BIN", "rsync")
RESTIC_BIN = _env("BACKUPKRAKE_RESTIC_BIN", "restic")

MAX_PARALLEL = int(_env("BACKUPKRAKE_MAX_PARALLEL", "2"))
ADMIN_USER = _env("BACKUPKRAKE_ADMIN_USER", "admin")
ADMIN_PASSWORD = _env("BACKUPKRAKE_ADMIN_PASSWORD", "")
# Disable the built-in scheduler (used by tests).
SCHEDULER_ENABLED = _env("BACKUPKRAKE_SCHEDULER", "1") == "1"

SNAPSHOT_FMT = "%Y-%m-%d_%H%M%S"


def ensure_dirs() -> None:
    for d in (DATA_DIR, HOSTS_DIR, RESTORE_DIR, CONFIG_DIR, LOG_DIR, SSH_DIR, RESTIC_CACHE):
        d.mkdir(parents=True, exist_ok=True)
    SSH_DIR.chmod(0o700)
