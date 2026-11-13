import asyncio
import os
import time

from app import backup, db, offsite
from app.main import DEFAULT_HOST


def test_offsite_roundtrip(tmp_path, monkeypatch):
    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "a.conf").write_text("one\n")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    host_id = db.create_host({**DEFAULT_HOST, "name": "offsite-h", "address": "x", "paths": "/etc",
                              "excludes": "", "acls_xattrs": 0})
    assert db.get_run(asyncio.run(backup.run_backup(host_id)))["status"] == "success"

    repo = tmp_path / "repo"
    offsite.save_settings({**offsite.DEFAULTS, "repository": str(repo), "password": "s3cret"})
    rid = asyncio.run(offsite.run_simple("offsite-init", ["init"]))
    assert db.get_run(rid)["status"] == "success"

    rid = asyncio.run(offsite.run_offsite())
    r = db.get_run(rid)
    log = open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()
    assert r["status"] == "success", log

    # second local snapshot, second upload must use the first as parent
    time.sleep(1.1)
    (client / "etc" / "b.conf").write_text("two\n")
    asyncio.run(backup.run_backup(host_id))
    rid = asyncio.run(offsite.run_offsite())
    log = open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()
    assert db.get_run(rid)["status"] == "success", log
    assert "--parent" in log
    assert "1 unmodified" in log  # a.conf not re-read despite living in a new hardlinked dir

    snaps = asyncio.run(offsite.list_snapshots())
    mine = [s for s in snaps if s["hostname"] == "offsite-h"]
    assert len(mine) == 2
    assert mine[0]["local_snapshot"] == backup.latest_snapshot("offsite-h")

    # restore the newest one and compare
    target = tmp_path / "restore"
    rid = asyncio.run(offsite.run_simple("offsite-restore", ["restore", mine[0]["id"], "--target", str(target)]))
    assert db.get_run(rid)["status"] == "success"
    found = list(target.rglob("b.conf"))
    assert found and found[0].read_text() == "two\n"


def test_offsite_includes_config(tmp_path, monkeypatch):
    import sqlite3
    import stat

    from app import config

    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "x").write_text("x")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    host_id = db.create_host({**DEFAULT_HOST, "name": "cfg-h", "address": "x", "paths": "/etc",
                              "excludes": "", "acls_xattrs": 0})
    asyncio.run(backup.run_backup(host_id))
    (config.RESTIC_CACHE / "junk").write_text("cache")

    repo = tmp_path / "repo"
    offsite.save_settings({**offsite.DEFAULTS, "repository": str(repo), "password": "pw"})
    asyncio.run(offsite.run_simple("offsite-init", ["init"]))
    rid = asyncio.run(offsite.run_offsite())
    r = db.get_run(rid)
    assert r["status"] == "success", open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()
    assert "+ config uploaded" in r["summary"]

    snaps = asyncio.run(offsite.list_snapshots())
    cfg = [s for s in snaps if s["hostname"] == offsite.CONFIG_HOST]
    assert len(cfg) == 1 and "config" in cfg[0]["tags"]

    target = tmp_path / "restored"
    rid = asyncio.run(offsite.run_simple("offsite-restore", ["restore", cfg[0]["id"], "--target", str(target)]))
    assert db.get_run(rid)["status"] == "success"
    dbfile = next(target.rglob(config.DB_PATH.name))
    root = dbfile.parent
    con = sqlite3.connect(dbfile)
    names = {row[0] for row in con.execute("SELECT name FROM hosts")}
    con.close()
    assert "cfg-h" in names
    assert (root / "ssh" / "id_ed25519").read_bytes() == config.SSH_KEY.read_bytes()
    assert (root / "ssh" / "id_ed25519_restore").exists()
    assert stat.S_IMODE((root / "ssh" / "id_ed25519").stat().st_mode) == 0o600
    assert (root / "secret_key").exists() and (root / "logs").is_dir()
    assert not (root / "restic-cache").exists()
    assert not list(root.glob("*.db-wal"))

    # second run reuses the config snapshot as parent; opting out skips it
    rid = asyncio.run(offsite.run_offsite())
    assert "--host _backupkrake-config" in open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()
    offsite.save_settings({**offsite.get_settings(), "include_config": False})
    rid = asyncio.run(offsite.run_offsite())
    log = open(f"{os.environ['BACKUPKRAKE_CONFIG_DIR']}/logs/{rid}.log").read()
    assert "--host _backupkrake-config" not in log and "configuration: uploading" not in log
    assert "+ config" not in db.get_run(rid)["summary"]


def test_sftp_repository_uses_offsite_key_and_known_hosts():
    import shlex
    from app import config, offsite
    argv = offsite.restic_argv({"repository": "sftp://u1@box.example:23//backupkrake"}, "snapshots")
    assert argv[0] == config.RESTIC_BIN and argv[-1] == "snapshots"
    opt = argv[argv.index("-o") + 1]
    assert opt.startswith("sftp.args=")
    ssh_args = shlex.split(opt.split("=", 1)[1])
    assert ssh_args[ssh_args.index("-i") + 1] == str(config.OFFSITE_KEY)
    assert f"UserKnownHostsFile={config.KNOWN_HOSTS}" in ssh_args
    assert "StrictHostKeyChecking=accept-new" in ssh_args
    # Other backends get no sftp options.
    assert offsite.restic_argv({"repository": "s3:https://x/y"}, "init") == [config.RESTIC_BIN, "init"]
