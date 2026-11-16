from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import scheduler

TZ = ZoneInfo("Europe/Berlin")
MONDAY = datetime(2026, 11, 16, 0, 0, tzinfo=TZ)


def fire_days(expr: str, n: int = 7) -> list[str]:
    t = scheduler.cron_trigger(expr)
    t.timezone = TZ
    out, prev = [], None
    now = MONDAY
    for _ in range(n):
        nxt = t.get_next_fire_time(prev, now)
        out.append(nxt.strftime("%a"))
        prev, now = nxt, nxt.replace(second=1)
    return out


def test_weekdays_follow_cron_not_apscheduler():
    # standard cron: 0 and 7 are Sunday (APScheduler 3 alone would make 0 a Monday)
    assert fire_days("0 6 * * 0", 2) == ["Sun", "Sun"]
    assert fire_days("0 6 * * 7", 1) == ["Sun"]
    assert fire_days("0 2 * * 1-5", 5) == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert fire_days("0 2 * * 6,0", 2) == ["Sat", "Sun"]
    assert fire_days("0 2 * * 5-7", 3) == ["Fri", "Sat", "Sun"]
    assert fire_days("0 2 * * */2", 4) == ["Tue", "Thu", "Sat", "Sun"]
    assert fire_days("0 2 * * mon-fri", 5) == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert fire_days("0 2 * * SUN", 1) == ["Sun"]
    assert fire_days("0 2 * * *", 3) == ["Mon", "Tue", "Wed"]
    assert fire_days("0 3 1 * *", 1) == ["Tue"]  # 1 December 2026


@pytest.mark.parametrize("expr", ["0 2 * * 8", "0 2 * * 5-1", "0 2 * *", "0 2 * * */0", "61 2 * * *", "0 2 * * xyz"])
def test_invalid(expr):
    assert scheduler.validate_cron(expr)


def test_empty_is_manual():
    assert scheduler.validate_cron("  ") is None
