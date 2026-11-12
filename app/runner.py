"""Run bookkeeping: run records, per-run log files, subprocess streaming, cancellation."""
import asyncio
import logging
import os
import shlex
from collections import deque
from datetime import datetime

from . import config, db

log = logging.getLogger("backupkrake")

# Keep references to background tasks so they are not garbage-collected.
_tasks: set[asyncio.Task] = set()
_contexts: dict[int, "RunContext"] = {}
_semaphore: asyncio.Semaphore | None = None
# Async callables run (in the background) with the run id whenever a run finishes.
finish_hooks: list = []


class Cancelled(Exception):
    pass


def semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.MAX_PARALLEL)
    return _semaphore


def spawn(coro) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def log_path(run_id: int):
    return config.LOG_DIR / f"{run_id}.log"


class RunContext:
    """Owns one run row and its log file."""

    def __init__(self, kind: str, host: dict | None, trigger: str):
        self.run_id = db.create_run(kind, host, trigger)
        self.cancelled = False
        self.stop_reason: str | None = None  # set when stopped automatically (e.g. window ended)
        self.finished = False
        self.proc: asyncio.subprocess.Process | None = None
        self._fh = open(log_path(self.run_id), "a", buffering=1, encoding="utf-8", errors="replace")
        _contexts[self.run_id] = self

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        for line in msg.rstrip("\n").split("\n"):
            self._fh.write(f"[{stamp}] {line}\n")

    def set_running(self) -> None:
        db.update_run(self.run_id, status="running")

    def stop(self, reason: str | None = None) -> None:
        """Abort the running command. With a reason the run ends as 'interrupted', else 'cancelled'."""
        if self.finished:
            return
        self.cancelled = True
        self.stop_reason = reason
        self.log(f"=== stopping: {reason}" if reason else "=== cancel requested")
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass

    def finish(self, status: str, summary: str = "", **fields) -> None:
        if self.finished:
            return
        self.finished = True
        if self.cancelled and status != "success":
            status = "interrupted" if self.stop_reason else "cancelled"
        self.log(f"=== finished: {status}{' - ' + summary if summary else ''}")
        db.update_run(self.run_id, status=status, summary=summary, finished=db.now(), **fields)
        self._fh.close()
        _contexts.pop(self.run_id, None)
        for hook in finish_hooks:
            try:
                spawn(hook(self.run_id))
            except RuntimeError:  # no running event loop
                log.warning("finish hook skipped for run %s", self.run_id)

    async def exec(
        self,
        argv: list[str],
        cwd: str | None = None,
        env: dict | None = None,
        tail: int = 300,
        uninterruptible: bool = False,
    ) -> tuple[int, list[str]]:
        """Run a command, streaming output to the log. Returns (exit code, last lines).

        Uninterruptible commands (e.g. a post-command that restarts a service) run
        even after a cancel/stop and are never terminated by one.
        """
        if self.cancelled and not uninterruptible:
            raise Cancelled()
        self.log("$ " + shlex.join(argv))
        full_env = None
        if env is not None:
            full_env = dict(os.environ)
            full_env.update(env)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=full_env,
            limit=1024 * 1024,
        )
        if not uninterruptible:
            self.proc = proc
        lines: deque[str] = deque(maxlen=tail)
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            lines.append(line)
            self.log(line)
        rc = await proc.wait()
        if not uninterruptible:
            self.proc = None
            if self.cancelled:
                raise Cancelled()
        return rc, list(lines)


def cancel(run_id: int) -> bool:
    ctx = _contexts.get(run_id)
    if not ctx:
        return False
    ctx.stop()
    return True


def is_active(run_id: int) -> bool:
    return run_id in _contexts


def read_log(run_id: int, offset: int = 0, max_bytes: int = 256 * 1024) -> tuple[str, int]:
    p = log_path(run_id)
    if not p.exists():
        return "", offset
    with open(p, "rb") as fh:
        size = fh.seek(0, 2)
        if offset > size:
            offset = 0
        fh.seek(offset)
        data = fh.read(max_bytes)
    return data.decode("utf-8", errors="replace"), offset + len(data)
