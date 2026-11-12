"""Grandfather-father-son retention selection (pure function, easy to test)."""
from datetime import datetime
from typing import Callable, Hashable


def select_keep(
    stamps: list[datetime],
    keep_last: int = 0,
    keep_daily: int = 0,
    keep_weekly: int = 0,
    keep_monthly: int = 0,
) -> set[datetime]:
    """Return the subset of ``stamps`` to keep.

    For each policy, the newest snapshot in each of the N most recent
    day/ISO-week/month buckets is kept. The newest snapshot is always kept.
    """
    ordered = sorted(set(stamps), reverse=True)
    if not ordered:
        return set()
    keep = set(ordered[: max(keep_last, 1)])

    policies: list[tuple[int, Callable[[datetime], Hashable]]] = [
        (keep_daily, lambda d: d.date()),
        (keep_weekly, lambda d: tuple(d.isocalendar())[:2]),
        (keep_monthly, lambda d: (d.year, d.month)),
    ]
    for count, bucket in policies:
        seen: set = set()
        for d in ordered:
            if len(seen) >= count:
                break
            b = bucket(d)
            if b not in seen:
                seen.add(b)
                keep.add(d)
    return keep
