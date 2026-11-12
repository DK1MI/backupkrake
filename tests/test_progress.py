"""Progress parsing and live progress during runs."""
import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import backup, db, offsite, progress, runner
from app.main import DEFAULT_HOST, app


# --- parsers ------------------------------------------------------------------

@pytest.mark.parametrize("line,nbytes", [
    ("      8,000,000  20%    8.64MB/s    0:00:00 (xfr#8, to-chk=32/41)", 8_000_000),
    ("      8.000.000B  20% 8.641.249B/s    0:00:00 (xfr#8, to-chk=32/41)", 8_000_000),   # rsync 3.4+, de locale
    ("      8'000'000  20%    8.64MB/s    0:00:00 (xfr#8, to-chk=32/41)", 8_000_000),
])
def test_rsync_line_formats(line, nbytes):
    t = progress.Tracker("rsync")
    assert t.feed(line)
    s = t.state
    assert s["bytes"] == nbytes and s["files_transferred"] == 8
    assert s["files_checked"] == 9 and s["files_total"] == 41 and not s["estimate"]
    assert s["pct"] == pytest.approx(100 * 9 / 41)


def test_rsync_percent_is_files_not_bytes():
    """An incremental run with few changes: rsync says 5 %, but every file was checked."""
    t = progress.Tracker("rsync")
    t.feed("      1,000,000   2%  0.97GB/s    0:00:00 (xfr#1, to-chk=17/41)")
    assert t.state["pct"] == pytest.approx(100 * 24 / 41)
    t.feed("      2,000,000   5%  1.97GB/s    0:00:00 (xfr#2, to-chk=0/41)")
    assert t.state["pct"] == pytest.approx(100 * 24 / 41)  # early "0 left" is not trusted...
    t.done()
    assert t.state["pct"] == 100.0 and t.state["files_checked"] == 41  # ...exit code 0 is


def test_rsync_incremental_recursion_uses_previous_file_count():
    t = progress.Tracker("rsync", expected_files=10_000)
    t.feed("  1,000  0%  1.00kB/s  0:00:01 (xfr#1, ir-chk=900/1000)")
    s = t.state
    assert s["estimate"] and s["files_total"] == 10_000 and s["pct"] == pytest.approx(1.0)
    # without a previous run the discovered total is used
    t = progress.Tracker("rsync")
    t.feed("  1,000  0%  1.00kB/s  0:00:01 (xfr#1, ir-chk=900/1000)")
    assert t.state["pct"] is None and t.state["files_checked"] == 100 and t.state["estimate"]
    # the bogus "ir-chk=0/1" at the very start must not read as done
    t.feed("  0  0%  0.00kB/s  0:00:00 (xfr#0, ir-chk=0/1)")
    assert t.state["pct"] is None


def test_rsync_first_line_without_counters_and_non_progress():
    t = progress.Tracker("rsync")
    assert t.feed("         32,768   0%    0.00kB/s    0:00:00  ")
    assert t.state["pct"] is None and t.state["bytes"] == 32768
    for other in ("Number of files: 41 (reg: 40, dir: 1)", ">f+++++++++ etc/x", "", "sent 1,234 bytes"):
        assert not t.feed(other)


@pytest.mark.parametrize("line,pct,files,total,eta", [
    ("[0:05] 40 files 38.147 MiB, total 40 files 38.147 MiB, 0 errors", None, 40, 40, None),
    ("[0:01] 12.34%  5 files 4.700 MiB, total 40 files 38.000 MiB, 0 errors ETA 0:07", 12.34, 5, 40, 7),
    ("[1:02:03] 50.00%  20 files/dirs 19.000 MiB, total 40 files/dirs 38.000 MiB, 0 errors ETA 1:00:00", 50.0, 20, 40, 3600),
])
def test_restic_lines(line, pct, files, total, eta):
    t = progress.Tracker("restic", label="web1")
    assert t.feed(line)
    s = t.state
    assert s["pct"] == pct and s["files_checked"] == files and s["files_total"] == total and s["eta"] == eta
    assert s["label"] == "web1" and s["kind"] == "restic"
    assert not t.feed("Files:          40 new,     0 changed,     0 unmodified")


def test_speed_and_eta():
    t = progress.Tracker("rsync")
    t.started -= 20
    t.samples.append((time.monotonic() - 4, 0))
    t.feed("  4,000,000  10%  1.00MB/s  0:00:04 (xfr#4, to-chk=50/100)")
    assert t.state["speed"] == pytest.approx(1_000_000, rel=0.1)
    assert t.state["eta"] == pytest.approx(20, abs=2)  # 50 % after ~20 s
    assert "50%" in t.summary() and "50/100 files" in t.summary()


# --- live progress during runs ---------------------------------------------------

def log_of(rid):
    return open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()


@pytest.fixture
def slow_host(tmp_path, monkeypatch, request):
    client = tmp_path / "client"
    (client / "data").mkdir(parents=True)
    for i in range(30):
        (client / "data" / f"f{i:02d}").write_bytes(os.urandom(100_000))
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    name = "p-" + request.node.name[:30].replace("_", "-")
    return db.get_host(db.create_host({**DEFAULT_HOST, "name": name, "address": "c", "paths": "/data",
                                       "excludes": "", "acls_xattrs": 0, "bwlimit": "600K"}))


def test_live_progress_during_backup(slow_host):
    seen = []

    async def go():
        task = asyncio.create_task(backup.run_backup(slow_host["id"]))
        rid = None
        for _ in range(100):
            await asyncio.sleep(0.2)
            cur = db.last_run(slow_host["id"])
            if cur and runner.is_active(cur["id"]):
                rid = cur["id"]
                p = runner.get_progress(rid)
                if p and p.get("pct") is not None:
                    seen.append(dict(p))
            if task.done():
                break
        return await task, rid

    rid, _ = asyncio.run(go())
    assert db.get_run(rid)["status"] == "success"
    assert seen, "no live progress observed"
    mid = [p for p in seen if 0 < p["pct"] < 100]
    assert mid, seen[-3:]
    assert mid[-1]["files_total"] == 31 and mid[-1]["bytes"] > 0
    exact = [p["pct"] for p in seen if not p["estimate"]]
    assert exact == sorted(exact)  # exact progress never goes backwards
    log = log_of(rid)
    assert "to-chk=" not in log           # raw progress lines are not logged
    assert "progress: 100%, 31/31 files" in log  # final summary is
    assert db.get_run(rid)["stats"]["files"] == 31  # --stats still parsed
    assert runner.get_progress(rid) is None  # cleared once finished


def test_progress_in_api_and_offsite(slow_host, tmp_path):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        r = c.post(f"/hosts/{slow_host['id']}/backup", follow_redirects=False)
        url = r.headers["location"]
        got = None
        for _ in range(100):
            data = c.get(url + "/log?offset=0").json()
            if data["progress"] and data["progress"].get("pct") is not None:
                got = data["progress"]
            if not data["active"] and data["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        assert got and got["kind"] == "rsync"
        assert data["progress"] is None and data["status"] == "success"
        assert 'id="progress"' in c.get(url).text

    if db._conn is None:  # closed by the TestClient shutdown
        db.init()
    # restic prints progress every 2 s; slow the upload down so there is something to report
    offsite.save_settings({**offsite.DEFAULTS, "repository": str(tmp_path / "repo"), "password": "pw",
                           "include_config": False, "limit_upload": 400})
    asyncio.run(offsite.run_simple("offsite-init", ["init"]))
    rid = asyncio.run(offsite.run_offsite())
    log = log_of(rid)
    assert db.get_run(rid)["status"] == "success", log
    assert f"progress: {slow_host['name']}" in log
