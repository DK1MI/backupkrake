from datetime import datetime, timedelta

from app import usage

MiB, GiB = 1024 ** 2, 1024 ** 3
NOW = datetime(2026, 11, 15, 12, 0).astimezone()


def hist(*added, total=50 * GiB, first_full=True):
    out = []
    for i, a in enumerate(added):
        full = first_full and i == 0
        out.append({"run_id": i + 1, "snapshot": f"s{i}", "date": NOW - timedelta(days=len(added) - i),
                    "added": a, "total": total, "full": full})
    return out


def test_chart_scales_to_incremental_backups_and_caps_full_copies():
    c = usage.chart(hist(48 * GiB, 200 * MiB, 900 * MiB, 0))
    full, small, big, zero = c["bars"]
    assert full["capped"] and full["label"]["text"] == "full copy 48.0 GiB"
    assert not big["capped"] and big["label"] is None
    # the scale is set by the incremental backups (900 MiB -> 1000 MiB axis), not the 48 GiB copy
    assert c["ticks"][-1]["label"] == "1000 MiB"
    assert zero["path"] == ""  # nothing added: no bar, but still a hover target
    assert "0 B added" in zero["tip"] and "(full copy)" in full["tip"]
    # bars stay inside the plot
    for b in c["bars"]:
        assert c["left"] <= b["hit_x"] and b["hit_x"] + b["hit_w"] <= c["right"] + 0.01


def test_chart_with_only_full_copies_and_no_history():
    assert usage.chart([]) is None
    c = usage.chart(hist(3 * GiB))
    assert not c["bars"][0]["capped"]
    assert c["ticks"][-1]["label"].endswith("GiB")


def test_summary():
    h = hist(48 * GiB, 100 * MiB, 300 * MiB)
    s = usage.summary(h, now=NOW)
    assert s["latest_total"] == 50 * GiB
    assert s["added_30d"] == 400 * MiB  # the full copy isn't growth
    assert s["runs_30d"] == 3
    assert s["avg_added"] == 200 * MiB
    assert usage.summary([], now=NOW) == {}


def test_history_from_runs(tmp_path, monkeypatch):
    import asyncio

    from app import backup, db
    from app.main import DEFAULT_HOST
    client = tmp_path / "client"
    (client / "etc").mkdir(parents=True)
    (client / "etc" / "big").write_bytes(b"x" * 2 * MiB)
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {client}")
    host_id = db.create_host({**DEFAULT_HOST, "name": "usage-h", "address": "x", "paths": "/etc",
                              "excludes": "", "acls_xattrs": 0})
    asyncio.run(backup.run_backup(host_id))
    h = usage.backup_history(host_id)
    assert len(h) == 1 and h[0]["full"] and h[0]["added"] >= 2 * MiB
