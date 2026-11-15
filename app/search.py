"""Find files across all snapshots of a host.

Snapshots share unchanged files through hard links, so a file that is the same
inode in several snapshots is the *same version*. Grouping by inode turns
"found in 30 snapshots" into the few real versions, each with the range of
snapshots that contain it.

Like browse.py, the walk uses lstat only and never follows symlinks. It runs in a
thread and stops at a result or time limit so a huge host can't hang the page.
"""
import fnmatch
import os
import stat
import time
from datetime import datetime

from . import backup, browse

MAX_PATHS = 200          # distinct matching paths
TIME_BUDGET = 30.0       # seconds


def _matcher(query: str):
    """Glob if the query has wildcards (case-insensitive), else substring of the name.
    A query containing '/' is matched against the whole relative path."""
    q = query.strip().lower()
    on_path = "/" in q
    if any(c in q for c in "*?["):
        pat = q.lstrip("/")
        def match(name: str, rel: str) -> bool:
            if not on_path:
                return fnmatch.fnmatchcase(name.lower(), pat)
            # "nginx/*.conf" matches from any folder level, like find -path '*/nginx/*.conf'
            r = rel.lower()
            return fnmatch.fnmatchcase(r, pat) or fnmatch.fnmatchcase(r, "*/" + pat)
    else:
        def match(name: str, rel: str) -> bool:
            return q.strip("/") in (rel if on_path else name).lower()
    return match


def search(host_name: str, query: str, within: str = "", max_paths: int = MAX_PATHS,
           time_budget: float = TIME_BUDGET) -> dict:
    """Returns {"results": [{"path", "kind", "versions": [...]}], "snapshots": n,
    "searched": n, "truncated": reason | None}. Versions are newest first, each with
    size, mtime, the newest and oldest snapshot containing it and how many do."""
    if not query.strip():
        return {"results": [], "snapshots": 0, "searched": 0, "truncated": None}
    match = _matcher(query)
    within_parts = browse.clean_rel(within)
    snaps = backup.list_snapshots(host_name)  # newest first
    deadline = time.monotonic() + time_budget
    found: dict[str, dict] = {}   # rel path -> {"kind", "versions": {inode: {...}}}
    truncated = None
    searched = 0

    for snap in snaps:
        try:
            root = backup.snapshot_path(host_name, snap)
            start = browse.resolve(root, "/".join(within_parts))
        except (FileNotFoundError, ValueError):
            searched += 1
            continue
        stack = [(start, "/".join(within_parts))]
        while stack:
            if time.monotonic() > deadline:
                truncated = f"stopped after {time_budget:.0f} seconds"
                break
            path, rel = stack.pop()
            try:
                entries = list(os.scandir(path))
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue  # pruned meanwhile, or unreadable
            for e in entries:
                erel = f"{rel}/{e.name}" if rel else e.name
                try:
                    st = e.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                if is_dir:
                    stack.append((e.path, erel))
                if not match(e.name, erel):
                    continue
                entry = found.get(erel)
                if entry is None:
                    if len(found) >= max_paths:
                        truncated = truncated or f"more than {max_paths} matching paths; refine the search"
                        continue
                    kind = "dir" if is_dir else "file" if stat.S_ISREG(st.st_mode) else \
                        "link" if stat.S_ISLNK(st.st_mode) else "other"
                    entry = found[erel] = {"path": erel, "kind": kind, "versions": {}}
                # Directories are never hard-linked, so each snapshot's directory is its own
                # "version"; group them by mtime instead.
                key = st.st_mtime_ns if is_dir else (st.st_dev, st.st_ino)
                v = entry["versions"].get(key)
                if v is None:
                    entry["versions"][key] = {
                        "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime),
                        "newest": snap, "oldest": snap, "count": 1,
                    }
                else:
                    v["oldest"] = snap  # snapshots are walked newest first
                    v["count"] += 1
        searched += 1
        if truncated and truncated.startswith("stopped"):
            break

    results = []
    for entry in found.values():
        versions = sorted(entry["versions"].values(), key=lambda v: v["newest"], reverse=True)
        results.append({"path": entry["path"], "kind": entry["kind"], "versions": versions})
    results.sort(key=lambda r: (r["kind"] == "dir", r["path"].lower()))
    return {"results": results, "snapshots": len(snaps), "searched": searched, "truncated": truncated}
