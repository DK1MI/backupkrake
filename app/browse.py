"""Safe, read-only browsing of snapshot contents.

Snapshots contain symlinks pointing at absolute paths of the *client*; following
them on the server would expose the container's own filesystem. Every path
component is therefore checked with lstat and symlinks are never followed.
"""
import os
import stat
import subprocess
from datetime import datetime
from pathlib import Path


class UnsafePath(ValueError):
    pass


def clean_rel(rel: str) -> list[str]:
    # Split manually: PurePosixPath keeps a leading "//" as a root component.
    parts = [p for p in (rel or "").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise UnsafePath("'..' is not allowed")
    return parts


def resolve(root: Path, rel: str, allow_final_symlink: bool = False) -> Path:
    parts = clean_rel(rel)
    cur = root
    for i, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            raise FileNotFoundError(rel) from None
        if stat.S_ISLNK(st.st_mode) and not (allow_final_symlink and i == len(parts) - 1):
            raise UnsafePath("path goes through a symlink")
    return cur


def entry_info(path: Path) -> dict:
    st = os.lstat(path)
    mode = st.st_mode
    kind = "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "link" if stat.S_ISLNK(mode) else "other"
    info = {
        "name": path.name,
        "kind": kind,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime),
        "mode": stat.filemode(mode),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "target": os.readlink(path) if kind == "link" else None,
    }
    return info


def listdir(path: Path) -> list[dict]:
    entries = [entry_info(Path(e.path)) for e in os.scandir(path)]
    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
    return entries


def tar_stream(path: Path, chunk: int = 256 * 1024):
    """Yield a gzip'd tar of ``path`` (symlinks archived as links, never followed)."""
    proc = subprocess.Popen(
        ["tar", "--numeric-owner", "-czf", "-", "-C", str(path.parent), path.name],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(chunk)
            if not data:
                break
            yield data
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
