import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import backup, browse, db, search
from app.main import DEFAULT_HOST, app


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    """Four snapshots: report.txt changes once (v1 in snaps 1-2, v2 in 3-4),
    notes.md never changes, Invoice-2026.PDF appears in snap 3."""
    import os
    client = tmp_path_factory.mktemp("client")
    (client / "home" / "alice" / "docs").mkdir(parents=True)
    (client / "etc" / "nginx").mkdir(parents=True)
    (client / "home" / "alice" / "docs" / "report.txt").write_text("version one\n")
    (client / "home" / "alice" / "docs" / "notes.md").write_text("notes\n")
    (client / "etc" / "nginx" / "site.conf").write_text("server {}\n")
    old = os.environ.get("FAKESSH_FORCED")
    os.environ["FAKESSH_FORCED"] = f"rrsync -ro {client}"
    host_id = db.create_host({**DEFAULT_HOST, "name": "search-h", "address": "x", "paths": "/home\n/etc",
                              "excludes": "", "acls_xattrs": 0, "keep_last": 10})
    snaps = []
    for i in range(4):
        if i == 2:
            (client / "home" / "alice" / "docs" / "report.txt").write_text("version two, longer\n")
            (client / "home" / "alice" / "docs" / "Invoice-2026.PDF").write_text("pdf\n")
        time.sleep(1.1)
        run = db.get_run(asyncio.run(backup.run_backup(host_id)))
        assert run["status"] == "success"
        snaps.append(run["snapshot"])
    if old is None:
        del os.environ["FAKESSH_FORCED"]
    else:
        os.environ["FAKESSH_FORCED"] = old
    return host_id, snaps


def test_versions_are_grouped_by_inode(history):
    _, snaps = history
    res = search.search("search-h", "report")
    assert res["snapshots"] == 4 and res["searched"] == 4 and res["truncated"] is None
    [hit] = res["results"]
    assert hit["path"] == "home/alice/docs/report.txt" and hit["kind"] == "file"
    new, old = hit["versions"]
    assert (new["oldest"], new["newest"], new["count"]) == (snaps[2], snaps[3], 2)
    assert (old["oldest"], old["newest"], old["count"]) == (snaps[0], snaps[1], 2)
    assert new["size"] == len("version two, longer\n")

    [notes] = search.search("search-h", "notes.md")["results"]
    assert len(notes["versions"]) == 1 and notes["versions"][0]["count"] == 4


def test_patterns_paths_and_folders(history):
    # glob, case-insensitive
    assert [r["path"] for r in search.search("search-h", "*.pdf")["results"]] == ["home/alice/docs/Invoice-2026.PDF"]
    # a "/" matches against the path
    assert [r["path"] for r in search.search("search-h", "nginx/*.conf")["results"]] == ["etc/nginx/site.conf"]
    # directories are found too (listed after files)
    assert [r["kind"] for r in search.search("search-h", "docs")["results"]] == ["dir"]
    # restricted to a folder
    assert search.search("search-h", "site", within="home")["results"] == []
    assert len(search.search("search-h", "site", within="etc")["results"]) == 1
    with pytest.raises(browse.UnsafePath):
        search.search("search-h", "x", within="../..")
    assert search.search("search-h", "   ")["results"] == []


def test_limits(history):
    res = search.search("search-h", "*", max_paths=2)
    assert len(res["results"]) == 2 and "more than 2" in res["truncated"]
    res = search.search("search-h", "*", time_budget=0)
    assert res["truncated"].startswith("stopped")


def test_search_page(history):
    host_id, snaps = history
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pw"})
        page = c.get(f"/hosts/{host_id}").text
        assert f'action="/hosts/{host_id}/search"' in page
        r = c.get(f"/hosts/{host_id}/search", params={"q": "report"})
        assert r.status_code == 200
        assert "home/alice/docs/report.txt" in r.text and "2 versions" in r.text
        assert f"/hosts/{host_id}/download/{snaps[3]}?path=home/alice/docs/report.txt" in r.text
        assert f"/hosts/{host_id}/browse/{snaps[3]}?path=home/alice/docs" in r.text
        assert "Nothing found" in c.get(f"/hosts/{host_id}/search", params={"q": "nope-nothing"}).text
        assert "not allowed" in c.get(f"/hosts/{host_id}/search", params={"q": "x", "within": ".."}).text
