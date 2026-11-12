"""Live progress for running jobs, parsed from rsync / restic output.

rsync (--info=progress2) prints lines such as
    "  8,000,000  20%  8.64MB/s  0:00:00 (xfr#8, to-chk=32/41)"
Its percentage is *transferred bytes / total size*, which is meaningless for
incremental backups (a finished run with few changes shows "5%"). We therefore
measure progress as *files checked*: total - remaining from "to-chk"/"ir-chk".
"ir-chk" means rsync is still discovering files (incremental recursion), so the
total grows; the previous run's file count is used as a better estimate then.

restic (RESTIC_PROGRESS_FPS set, no terminal) prints lines such as
    "[0:05] 12.34%  5 files 4.7 MiB, total 40 files 38 MiB, 0 errors ETA 0:07"
"""
import re
import time
from collections import deque

_RSYNC_RE = re.compile(
    r"^\s*([\d.,']+)B?\s+(\d+)%\s+\S+\s+\d+:\d+:\d+"
    r"(?:\s+\(xfr#(\d+),\s*(ir-chk|to-chk)=(\d+)/(\d+)\))?\s*$"
)
_RESTIC_RE = re.compile(
    r"^\[(?:(\d+):)?(\d+):(\d+)\]\s+(?:([\d.]+)%\s+)?(\d+) files(?:/dirs)? ([\d.]+ [KMGTP]?i?B)"
    r"(?:, total (\d+) files(?:/dirs)? ([\d.]+ [KMGTP]?i?B))?.*?(?:ETA (\S+))?\s*$"
)
_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4, "PiB": 1024 ** 5}


def _int(digits: str) -> int:
    return int(re.sub(r"\D", "", digits) or 0)


def _size(text: str) -> int:
    num, unit = text.split()
    return int(float(num) * _UNITS.get(unit, 1))


def _clock(text: str) -> int | None:
    parts = [int(p) for p in re.findall(r"\d+", text or "")]
    if not parts:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


class Tracker:
    """Feed it output lines; it recognises progress lines and keeps the latest state."""

    def __init__(self, kind: str = "rsync", expected_files: int | None = None, label: str = ""):
        self.kind = kind
        self.expected_files = expected_files or None
        self.label = label
        self.started = time.monotonic()
        self.samples: deque[tuple[float, int]] = deque(maxlen=20)
        self.state: dict | None = None

    def feed(self, line: str) -> bool:
        m = (_RSYNC_RE if self.kind == "rsync" else _RESTIC_RE).match(line)
        if not m:
            return False
        if self.kind == "rsync":
            self._rsync(m)
        else:
            self._restic(m)
        return True

    def _speed(self, now: float, nbytes: int) -> float | None:
        self.samples.append((now, nbytes))
        t0, b0 = self.samples[0]
        return (nbytes - b0) / (now - t0) if now - t0 >= 2 else None

    def _rsync(self, m: re.Match) -> None:
        now = time.monotonic()
        nbytes = _int(m.group(1))
        transferred = int(m.group(3)) if m.group(3) else None
        state = {"label": self.label, "bytes": nbytes, "files_transferred": transferred,
                 "speed": self._speed(now, nbytes), "pct": None, "estimate": False, "eta": None}
        if m.group(4):
            remaining, discovered = int(m.group(5)), int(m.group(6))
            checked = max(0, discovered - remaining)
            if m.group(4) == "to-chk" and remaining == 0:
                # rsync's file checker runs ahead of the transfer and reports "0 left" early,
                # then goes back to the real count. Keep the previous figures; done() marks 100 %.
                prev = self.state or {}
                state.update(files_checked=prev.get("files_checked"), files_total=prev.get("files_total"),
                             pct=prev.get("pct"), estimate=prev.get("estimate", False))
            elif m.group(4) == "to-chk":
                # File list complete: exact.
                state.update(files_checked=checked, files_total=discovered,
                             pct=100.0 * checked / discovered if discovered else 100.0)
            elif self.expected_files:
                # Still discovering files: estimate against the previous run's file count.
                total = max(discovered, self.expected_files)
                state.update(files_checked=checked, files_total=total, estimate=True,
                             pct=min(99.0, 100.0 * checked / total))
            else:
                # Still discovering and nothing to compare with: no meaningful percentage.
                state.update(files_checked=checked, files_total=None, estimate=True)
        self._finish(state, now)

    def _restic(self, m: re.Match) -> None:
        now = time.monotonic()
        done_bytes = _size(m.group(6))
        state = {
            "label": self.label, "bytes": done_bytes, "speed": self._speed(now, done_bytes),
            "files_checked": int(m.group(5)),
            "files_total": int(m.group(7)) if m.group(7) else None,
            "bytes_total": _size(m.group(8)) if m.group(8) else None,
            "pct": float(m.group(4)) if m.group(4) else None,
            "estimate": False,
            "eta": _clock(m.group(9)) if m.group(9) else None,
        }
        self._finish(state, now)

    def _finish(self, state: dict, now: float) -> None:
        elapsed = now - self.started
        state["elapsed"] = int(elapsed)
        state["kind"] = self.kind
        pct = state.get("pct")
        if state.get("eta") is None and pct and 2 <= pct < 100 and elapsed > 10:
            state["eta"] = int(elapsed * (100 - pct) / pct)
        self.state = state

    def done(self) -> None:
        """The command finished successfully: everything was checked."""
        if self.state:
            self.state["pct"] = 100.0
            self.state["estimate"] = False
            self.state["eta"] = 0
            if self.state.get("files_total"):
                self.state["files_checked"] = self.state["files_total"]

    def summary(self) -> str:
        """One line for the run log."""
        s = self.state or {}
        parts = []
        if s.get("label"):
            parts.append(s["label"])
        if s.get("pct") is not None:
            parts.append(f"{s['pct']:.0f}%{' (estimate)' if s.get('estimate') else ''}")
        if s.get("files_total"):
            parts.append(f"{s.get('files_checked', 0):,}/{s['files_total']:,} files")
        parts.append(f"{s.get('bytes', 0) / 1024 ** 2:,.1f} MiB {'transferred' if self.kind == 'rsync' else 'read'}")
        if s.get("speed"):
            parts.append(f"{s['speed'] / 1024 ** 2:.2f} MiB/s")
        return "progress: " + ", ".join(parts)
