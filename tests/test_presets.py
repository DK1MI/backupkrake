import asyncio
import shutil
import sqlite3
import subprocess

import pytest

from app import backup, db, presets
from app.main import DEFAULT_HOST


@pytest.mark.parametrize("scope", ["system", "home"])
def test_presets_are_valid_shell_and_combine(scope):
    ps = presets.presets(scope)
    assert {p["id"] for p in ps} >= {"postgresql", "mysql", "sqlite", "docker-volumes", "compose-stop"}
    for p in ps:
        for cmd in (p["pre"], p["post"]):
            if cmd:
                r = subprocess.run(["sh", "-n", "-c", cmd], capture_output=True, text=True)
                assert r.returncode == 0, (p["id"], r.stderr)
                assert "{" not in cmd.replace("${MARIADB", "").replace("${", "")  # no unfilled template fields
    combined = " && ".join(p["pre"] for p in ps if p["available"])
    assert subprocess.run(["sh", "-n", "-c", combined]).returncode == 0
    available = {p["id"] for p in ps if p["available"]}
    assert available == ({"sqlite"} if scope == "home" else {p["id"] for p in ps})
    for p in ps:
        assert p["path"] in (presets.DUMP_PATH[scope], "/PATH/TO/PROJECT")


@pytest.mark.skipif(not shutil.which("sqlite3"), reason="needs the sqlite3 command")
def test_sqlite_preset_dump_ends_up_in_the_snapshot(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "app").mkdir(parents=True)
    con = sqlite3.connect(home / "app" / "data.db")
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES ('hello from the database')")
    con.commit()
    con.close()
    monkeypatch.setenv("FAKESSH_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FAKESSH_FORCED", raising=False)  # unrestricted key: pre-commands allowed

    p = next(p for p in presets.presets("home") if p["id"] == "sqlite")
    pre = p["pre"].replace("$HOME/PATH/TO/DATABASE.db", "$HOME/app/data.db")
    host_id = db.create_host({**DEFAULT_HOST, "name": "preset-sqlite", "address": "x", "user": "me",
                              "scope": "home", "paths": p["path"], "excludes": "", "acls_xattrs": 0,
                              "pre_cmd": pre})
    run = db.get_run(asyncio.run(backup.run_backup(host_id)))
    assert run["status"] == "success", run["summary"]
    dump = backup.snapshot_path("preset-sqlite", run["snapshot"]) / ".backupkrake-dumps" / "app.db"
    rows = sqlite3.connect(dump).execute("SELECT x FROM t").fetchall()
    assert rows == [("hello from the database",)]


def test_failing_dump_aborts_the_backup(tmp_path, monkeypatch):
    client = tmp_path / "client"
    (client / "var" / "backups").mkdir(parents=True)
    monkeypatch.delenv("FAKESSH_FORCED", raising=False)
    monkeypatch.delenv("FAKESSH_HOME", raising=False)
    p = next(p for p in presets.presets("system") if p["id"] == "docker-postgres")
    # the container doesn't exist (or docker isn't installed): the dump must fail the backup
    pre = p["pre"].replace("/var/backups/backupkrake", str(client / "dumps")).replace("docker exec", "no-such-docker exec")
    host_id = db.create_host({**DEFAULT_HOST, "name": "preset-fail", "address": "x", "paths": str(client),
                              "excludes": "", "acls_xattrs": 0, "pre_cmd": pre})
    run = db.get_run(asyncio.run(backup.run_backup(host_id)))
    assert run["status"] == "failed" and "pre-command failed" in run["summary"]
    assert backup.list_snapshots("preset-fail") == []
