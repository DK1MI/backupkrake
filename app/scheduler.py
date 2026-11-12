"""Cron-style scheduling of host backups and the offsite job."""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import backup, config, db, notify, offsite, windows

log = logging.getLogger("backupkrake")
_sched: AsyncIOScheduler | None = None
OFFSITE_JOB = "offsite"
HOUSEKEEPING_JOB = "housekeeping"


def validate_cron(expr: str) -> str | None:
    """Return an error message, or None if the expression is valid (empty = manual only)."""
    if not expr.strip():
        return None
    try:
        CronTrigger.from_crontab(expr.strip())
    except ValueError as e:
        return str(e)
    return None


async def _backup_job(host_id: int) -> None:
    await backup.run_backup(host_id, trigger="schedule")


async def _offsite_job() -> None:
    await offsite.run_offsite(trigger="schedule")


def start() -> None:
    global _sched
    if not config.SCHEDULER_ENABLED:
        return
    _sched = AsyncIOScheduler()
    _sched.start()
    sync_all()
    # Overdue-host alerts + heartbeat: hourly, first run shortly after startup so an
    # outage of the server itself is reported as soon as it is back.
    _sched.add_job(
        notify.housekeeping, IntervalTrigger(hours=1), id=HOUSEKEEPING_JOB,
        next_run_time=datetime.now().astimezone() + timedelta(minutes=2),
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )


def shutdown() -> None:
    global _sched
    if _sched:
        _sched.shutdown(wait=False)
        _sched = None


def _job_id(host_id: int) -> str:
    return f"host-{host_id}"


def sync_host(host: dict) -> None:
    if not _sched:
        return
    jid = _job_id(host["id"])
    if _sched.get_job(jid):
        _sched.remove_job(jid)
    if host["enabled"] and host["schedule"].strip():
        _sched.add_job(
            _backup_job, CronTrigger.from_crontab(host["schedule"].strip()),
            id=jid, args=[host["id"]], coalesce=True, max_instances=1, misfire_grace_time=3600,
        )


def remove_host(host_id: int) -> None:
    if _sched and _sched.get_job(_job_id(host_id)):
        _sched.remove_job(_job_id(host_id))


def sync_offsite() -> None:
    if not _sched:
        return
    if _sched.get_job(OFFSITE_JOB):
        _sched.remove_job(OFFSITE_JOB)
    s = offsite.get_settings()
    if s["enabled"] and offsite.configured(s) and s["schedule"].strip():
        _sched.add_job(
            _offsite_job, CronTrigger.from_crontab(s["schedule"].strip()),
            id=OFFSITE_JOB, coalesce=True, max_instances=1, misfire_grace_time=3600,
        )


def sync_all() -> None:
    for host in db.list_hosts():
        try:
            sync_host(host)
        except ValueError as e:
            log.error("invalid schedule for %s: %s", host["name"], e)
    sync_offsite()


def times_outside_window(host: dict, count: int = 50) -> list[datetime]:
    """Upcoming scheduled fire times (of the next `count`) that fall outside the host's window."""
    if not (host["schedule"].strip() and host.get("backup_window", "").strip()):
        return []
    trigger = CronTrigger.from_crontab(host["schedule"].strip())
    ranges = windows.parse(host["backup_window"])
    out, prev, now = [], None, datetime.now().astimezone()
    for _ in range(count):
        t = trigger.get_next_fire_time(prev, prev or now)
        if t is None:
            break
        if not windows.state(ranges, t)[0]:
            out.append(t)
        prev = t + timedelta(seconds=1)
    return out


def next_run(host_id: int | None = None) -> datetime | None:
    if not _sched:
        return None
    job = _sched.get_job(_job_id(host_id) if host_id is not None else OFFSITE_JOB)
    return job.next_run_time if job else None
