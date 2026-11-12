"""Backup windows: daily time ranges in which scheduled backups may run.

Syntax: comma-separated "HH:MM-HH:MM" ranges in local time. A range whose end
is before its start crosses midnight ("22:00-06:00"). Empty = no restriction.
"""
import re
from datetime import date, datetime, time, timedelta

RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
Ranges = list[tuple[int, int]]  # (start minute, end minute) of day; end may be 1440


def parse(text: str) -> Ranges:
    ranges: Ranges = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = RANGE_RE.match(part)
        if not m:
            raise ValueError(f"“{part}” is not a time range like 22:00-06:00")
        sh, sm, eh, em = map(int, m.groups())
        if sh > 23 or sm > 59 or em > 59 or eh > 24 or (eh == 24 and em):
            raise ValueError(f"“{part}” contains an invalid time")
        start, end = sh * 60 + sm, eh * 60 + em
        if start == end % 1440:
            raise ValueError(f"“{part}” has the same start and end; leave the window empty for “any time”")
        ranges.append((start, end))
    return ranges


def normalize(text: str) -> str:
    return ", ".join(f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in parse(text))


def _segments(ranges: Ranges, day: date, tz) -> list[tuple[datetime, datetime]]:
    base = datetime.combine(day, time(0), tzinfo=tz)
    segs = []
    for start, end in ranges:
        s = base + timedelta(minutes=start)
        e = base + timedelta(minutes=end if end > start else end + 1440)
        segs.append((s, e))
    return segs


def state(ranges: Ranges, now: datetime) -> tuple[bool, datetime | None]:
    """(allowed now?, end of the current window or None if unrestricted/outside)."""
    if not ranges:
        return True, None
    segs = []
    for offset in (-1, 0, 1):
        segs += _segments(ranges, now.date() + timedelta(days=offset), now.tzinfo)
    segs.sort()
    end = None
    for s, e in segs:
        if s <= now < e and (end is None or e > end):
            end = e
    if end is None:
        return False, None
    # Merge adjacent/overlapping windows, e.g. "20:00-24:00, 00:00-06:00".
    extended = True
    while extended:
        extended = False
        for s, e in segs:
            if s <= end < e:
                end, extended = e, True
    return True, end
