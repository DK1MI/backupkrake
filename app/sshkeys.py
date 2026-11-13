"""Server SSH key management and helpers for talking to clients.

Hosts have a scope: "system" (log in as root, absolute paths) or "home" (log in as
a normal user, paths relative to the home directory; works with `rrsync -ro .` as
well as an unrestricted key, because both resolve relative paths against $HOME).

Two keys are used so that clients can grant them different rights:
  * backup key  -> authorized as `rrsync -ro /` (read-only)
  * restore key -> authorized as `rrsync -wo /` (write-only), optional per client
"""
import shlex
import subprocess
from pathlib import Path

from . import config


def _ensure(path: Path, comment: str) -> None:
    if not path.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
            check=True,
        )


def ensure_key() -> None:
    _ensure(config.SSH_KEY, "backupkrake")
    _ensure(config.RESTORE_KEY, "backupkrake-restore")
    _ensure(config.OFFSITE_KEY, "backupkrake-offsite")
    config.KNOWN_HOSTS.touch(exist_ok=True)


def _pub(path: Path) -> str:
    pub = path.with_suffix(".pub")
    return pub.read_text().strip() if pub.exists() else ""


def public_key() -> str:
    return _pub(config.SSH_KEY)


def restore_public_key() -> str:
    return _pub(config.RESTORE_KEY)


def offsite_public_key() -> str:
    return _pub(config.OFFSITE_KEY)


def rrsync_root(scope: str = "system") -> str:
    """Directory the forced command is locked to. ssh runs forced commands in the
    user's home directory, so "." restricts a non-root key to that home folder."""
    return "." if scope == "home" else "/"


def authorized_keys_line(restricted: bool = True, scope: str = "system") -> str:
    key = public_key()
    if restricted:
        return f'command="rrsync -ro {rrsync_root(scope)}",restrict {key}'
    return key


def restore_authorized_keys_line(scope: str = "system") -> str:
    return f'command="rrsync -wo {rrsync_root(scope)}",restrict {restore_public_key()}'


def ssh_options(host: dict, key: Path | None = None) -> list[str]:
    return [
        "-i", str(key or config.SSH_KEY),
        "-p", str(host["port"]),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={config.KNOWN_HOSTS}",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
    ]


def ssh_command_string(host: dict, key: Path | None = None) -> str:
    """Value for rsync's -e option."""
    return shlex.join([config.SSH_BIN] + ssh_options(host, key))


def ssh_argv(host: dict, remote_cmd: str) -> list[str]:
    return [config.SSH_BIN] + ssh_options(host) + [f"{host['user']}@{host['address']}", remote_cmd]


def remote_spec(host: dict, path: str) -> str:
    addr = host["address"]
    if ":" in addr and not addr.startswith("["):  # IPv6 literal
        addr = f"[{addr}]"
    return f"{host['user']}@{addr}:{path}"


def forget_host_key(host: dict) -> None:
    target = host["address"] if host["port"] == 22 else f"[{host['address']}]:{host['port']}"
    subprocess.run(
        ["ssh-keygen", "-R", target, "-f", str(config.KNOWN_HOSTS)],
        capture_output=True,
    )
