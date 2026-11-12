import io
import os
import tarfile
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(c):
    r = c.post("/login", data={"username": "admin", "password": "test-pw"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/healthz").json() == {"ok": True}


def test_bad_login(client):
    r = client.post("/login", data={"username": "admin", "password": "nope"})
    assert "Invalid username or password" in r.text


def test_full_flow(client, tmp_path, monkeypatch):
    src = tmp_path / "client"
    (src / "etc" / "sub").mkdir(parents=True)
    (src / "etc" / "motd").write_text("welcome\n")
    (src / "etc" / "sub" / "x.txt").write_text("x\n")
    os.symlink("/etc/passwd", src / "etc" / "pw-link")
    monkeypatch.setenv("FAKESSH_FORCED", f"rrsync -ro {src}")

    login(client)
    assert "No hosts yet" in client.get("/").text or "Hosts" in client.get("/").text
    for page in ("/setup", "/offsite", "/runs", "/hosts/new"):
        assert client.get(page).status_code == 200, page
    assert "rrsync -ro /" in client.get("/setup").text

    # validation errors
    r = client.post("/hosts/new", data={"name": "bad name", "address": "", "user": "root", "port": "22",
                                        "paths": "relative", "schedule": "nope", "keep_last": "1",
                                        "keep_daily": "0", "keep_weekly": "0", "keep_monthly": "0"})
    assert "Paths must be absolute" in r.text and "Schedule" in r.text

    form = {"name": "web1", "address": "web1.lan", "user": "root", "port": "22", "paths": "/etc\n/srv",
            "excludes": "", "schedule": "30 1 * * *", "enabled": "1", "keep_last": "3",
            "keep_daily": "7", "keep_weekly": "4", "keep_monthly": "12"}
    r = client.post("/hosts/new", data=form, follow_redirects=False)
    assert r.status_code == 303
    host_url = r.headers["location"]
    assert "web1" in client.get(host_url).text

    r = client.post(host_url + "/test")
    assert "Backups should work" in r.text

    r = client.post(host_url + "/backup", follow_redirects=False)
    run_url = r.headers["location"]
    assert run_url.startswith("/runs/")
    for _ in range(50):
        data = client.get(run_url + "/log?offset=0").json()
        if not data["active"] and data["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert data["status"] == "success", data["text"]
    assert "missing on client: /srv" in data["summary"]
    assert "Total file size" in data["text"]
    assert client.get(run_url).status_code == 200

    page = client.get(host_url).text
    assert "latest" in page
    hid = host_url.rsplit("/", 1)[1]
    from app import backup
    snap = backup.latest_snapshot("web1")

    r = client.get(f"/hosts/{hid}/browse/{snap}?path=etc")
    assert "motd" in r.text and "pw-link" in r.text and "/etc/passwd" in r.text
    r = client.get(f"/hosts/{hid}/download/{snap}?path=etc/motd")
    assert r.content == b"welcome\n"
    # symlinks are never followed
    assert client.get(f"/hosts/{hid}/download/{snap}?path=etc/pw-link").status_code == 400
    assert client.get(f"/hosts/{hid}/browse/{snap}?path=etc/pw-link/x").status_code == 400
    assert client.get(f"/hosts/{hid}/browse/{snap}?path=../../..").status_code == 400
    assert client.get(f"/hosts/{hid}/download/{snap}?path=//etc/passwd").status_code == 404
    assert client.get(f"/hosts/{hid}/browse/2020-01-01_000000").status_code == 404

    r = client.get(f"/hosts/{hid}/download/{snap}?path=etc")
    tf = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz")
    names = tf.getnames()
    assert "etc/motd" in names and "etc/sub/x.txt" in names
    assert tf.getmember("etc/pw-link").issym()

    # edit keeps name fixed
    r = client.post(host_url + "/edit", data={**form, "name": "renamed"})
    assert "cannot be changed" in r.text
    r = client.post(host_url + "/edit", data={**form, "schedule": ""}, follow_redirects=False)
    assert r.status_code == 303

    # delete snapshot, then host with data
    r = client.post(f"/hosts/{hid}/snapshots/{snap}/delete", follow_redirects=True)
    assert f"Snapshot {snap} deleted" in r.text
    r = client.post(f"/hosts/{hid}/delete", data={"delete_data": "1"}, follow_redirects=True)
    assert "all its snapshots deleted" in r.text
    assert not backup.host_dir("web1").exists()


def test_offsite_settings(client):
    login(client)
    r = client.post("/offsite", data={"enabled": "1", "repository": "", "password": "", "schedule": "0 5 * * *",
                                      "keep_daily": "7", "keep_weekly": "4", "keep_monthly": "12",
                                      "keep_yearly": "3", "limit_upload": "0"})
    assert "Repository and password are required" in r.text
    r = client.post("/offsite", data={"repository": "/tmp/x", "password": "pw", "schedule": "0 5 * * *",
                                      "keep_daily": "7", "keep_weekly": "4", "keep_monthly": "12",
                                      "keep_yearly": "3", "limit_upload": "0"}, follow_redirects=True)
    assert "Offsite settings saved" in r.text
    # empty password field keeps the stored password
    client.post("/offsite", data={"repository": "/tmp/x", "password": "", "schedule": "0 5 * * *",
                                  "keep_daily": "7", "keep_weekly": "4", "keep_monthly": "12",
                                  "keep_yearly": "3", "limit_upload": "0"})
    from app import offsite
    assert offsite.get_settings()["password"] == "pw"
    assert "pw" not in client.get("/offsite").text.replace("password", "")
