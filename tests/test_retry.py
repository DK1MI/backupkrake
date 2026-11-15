import asyncio
from datetime import datetime, timedelta

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import backup, db, notify, scheduler
from app.main import DEFAULT_HOST


@pytest.fixture
def offline_host(tmp_path, monkeypatch):
    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "f").write_text("x\n")
    flag = tmp_path / "offline"
    flag.touch()
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    monkeypatch.setenv("FAKESSH_OFFLINE", str(flag))
    return flag


def with_scheduler(coro_fn):
    """Run coro_fn() with a live scheduler (tests normally run without one)."""
    async def go():
        scheduler._sched = AsyncIOScheduler()
        scheduler._sched.start()
        try:
            return await coro_fn()
        finally:
            scheduler._sched.shutdown(wait=False)
            scheduler._sched = None
    return asyncio.run(go())


def make(name, **kw):
    return db.create_host({**DEFAULT_HOST, "name": name, "address": "x", "paths": "/etc", "excludes": "",
                           "acls_xattrs": 0, "schedule": "0 2 * * *", **kw})


def test_unreachable_without_retry_fails(offline_host):
    host_id = make("off-noretry")
    run = db.get_run(asyncio.run(backup.run_backup(host_id, trigger="schedule")))
    assert run["status"] == "failed"
    assert run["summary"] == "host unreachable (ssh: connect to host client port 22: No route to host)"
    assert notify.build_message(run, notify.get_settings())[0] == "failure"


def test_retry_until_host_is_back(offline_host):
    host_id = make("laptop", retry_minutes=30)

    async def scenario():
        scheduler.sync_host(db.get_host(host_id))
        rid = await backup.run_backup(host_id, trigger="schedule")
        run = db.get_run(rid)
        assert run["status"] == "skipped", run["summary"]
        assert run["summary"].startswith("host unreachable; retrying at ")
        assert run["stats"]["unreachable"] is True
        assert notify.build_message(run, notify.get_settings()) is None  # no failure notification
        at = scheduler.next_retry(host_id)
        assert at and timedelta(minutes=29) < at - datetime.now().astimezone() <= timedelta(minutes=30)

        # still offline at the retry: another retry is planned
        rid = await backup.run_backup(host_id, trigger="retry")
        assert db.get_run(rid)["status"] == "skipped" and scheduler.next_retry(host_id)

        # back online: the retry backs it up and no further retry is pending
        offline_host.unlink()
        rid = await backup.run_backup(host_id, trigger="retry")
        assert db.get_run(rid)["status"] == "success"
        assert scheduler.next_retry(host_id) is None

        # a manual "Back up now" while offline is an ordinary failure, no retry
        offline_host.touch()
        rid = await backup.run_backup(host_id, trigger="manual")
        assert db.get_run(rid)["status"] == "failed" and scheduler.next_retry(host_id) is None

    with_scheduler(scenario)


def test_retry_stops_before_next_run_and_at_window_end(offline_host):
    now = datetime.now().astimezone()
    soon = now + timedelta(minutes=20)
    # next regular run in 20 minutes: a 30-minute retry would come after it
    host_id = make("desk", retry_minutes=30, schedule=f"{soon.minute} {soon.hour} * * *")

    async def scenario():
        scheduler.sync_host(db.get_host(host_id))
        assert scheduler.schedule_retry(db.get_host(host_id), now) is None

        # window closes in 10 minutes: no retry after it
        end = now + timedelta(minutes=10)
        start = now - timedelta(hours=1)
        db.update_host(host_id, {**db.get_host(host_id), "schedule": "0 2 * * *",
                                 "backup_window": f"{start:%H:%M}-{end:%H:%M}"})
        scheduler.sync_host(db.get_host(host_id))
        assert scheduler.schedule_retry(db.get_host(host_id), now) is None

        # retries switched off in the host settings: a pending retry is dropped
        db.update_host(host_id, {**db.get_host(host_id), "backup_window": ""})
        assert scheduler.schedule_retry(db.get_host(host_id), now)
        db.update_host(host_id, {**db.get_host(host_id), "retry_minutes": 0})
        scheduler.sync_host(db.get_host(host_id))
        assert scheduler.next_retry(host_id) is None

    with_scheduler(scenario)


def test_auth_errors_are_not_offline(tmp_path, monkeypatch):
    # "Permission denied" is a configuration problem and must not be retried quietly
    monkeypatch.setenv("FAKESSH_FORCED", "sh -c 'echo \"root@client: Permission denied (publickey).\" >&2; exit 255'")
    host_id = make("noauth", retry_minutes=30)
    assert asyncio.run(backup.check_reachable(db.get_host(host_id))) is None
    run = db.get_run(asyncio.run(backup.run_backup(host_id, trigger="schedule")))
    assert run["status"] == "failed" and "unreachable" not in run["summary"]
