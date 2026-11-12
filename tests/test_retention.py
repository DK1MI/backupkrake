from datetime import datetime, timedelta

from app.retention import select_keep


def days(n, start=datetime(2026, 11, 12, 2, 0)):
    return [start - timedelta(days=i) for i in range(n)]


def test_newest_always_kept():
    stamps = days(5)
    assert select_keep(stamps) == {stamps[0]}


def test_keep_last():
    stamps = days(10)
    assert select_keep(stamps, keep_last=3) == set(stamps[:3])


def test_daily_one_per_day():
    base = datetime(2026, 11, 12, 2, 0)
    stamps = [base, base - timedelta(hours=1), base - timedelta(days=1), base - timedelta(days=1, hours=3)]
    keep = select_keep(stamps, keep_last=1, keep_daily=2)
    assert keep == {base, base - timedelta(days=1)}


def test_weekly_and_monthly():
    stamps = days(120)
    keep = select_keep(stamps, keep_last=1, keep_daily=7, keep_weekly=4, keep_monthly=4)
    assert set(stamps[:7]) <= keep
    months = sorted({(d.year, d.month) for d in stamps}, reverse=True)[:4]
    for m in months:  # newest snapshot of each of the 4 newest months is kept
        assert max(s for s in stamps if (s.year, s.month) == m) in keep
    weeks = sorted({tuple(d.isocalendar())[:2] for d in stamps}, reverse=True)[:4]
    for w in weeks:
        assert max(s for s in stamps if tuple(s.isocalendar())[:2] == w) in keep
    assert {(d.year, d.month) for d in keep} == set(months)
    assert len(keep) <= 7 + 4 + 4


def test_empty():
    assert select_keep([]) == set()
