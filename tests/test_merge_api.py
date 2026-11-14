"""三方合并 HTTP 端到端测试。"""
from __future__ import annotations

import os
import sqlite3
import time

os.environ.setdefault("DATA_DIR", "/tmp/reducer-test-data")

from app.config import DATA_DIR  # noqa: E402
from app.samples import (  # noqa: E402
    build_merge_base,
    build_merge_current,
    build_merge_revision,
)

SCHEMA = """\
CREATE TABLE dept (
    id INTEGER PRIMARY KEY,
    leader_id INTEGER REFERENCES employee(id),
    name TEXT NOT NULL
);
CREATE TABLE employee (
    id INTEGER PRIMARY KEY,
    dept_id INTEGER NOT NULL REFERENCES dept(id),
    name TEXT NOT NULL
);
"""


def _db_bytes(tmp_path, name, dept_rows, employee_rows):
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO dept (id, leader_id, name) VALUES (?, ?, ?)",
                     dept_rows)
    conn.executemany("INSERT INTO employee (id, dept_id, name) VALUES (?, ?, ?)",
                     employee_rows)
    conn.commit()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
    return path.read_bytes()


def _post(client, files):
    return client.post("/api/merge-jobs", files=files)


def _wait(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200
        job = r.json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("合并任务超时")


def _files(tmp_path, base, rev, cur):
    return {
        "base_db": ("base.db", base, "application/x-sqlite3"),
        "revision_db": ("revision.db", rev, "application/x-sqlite3"),
        "current_db": ("current.db", cur, "application/x-sqlite3"),
    }


def test_merge_sample_end_to_end(client, tmp_path):
    # 直接用内置样例的三个库
    b, r, c = tmp_path / "b.db", tmp_path / "r.db", tmp_path / "c.db"
    build_merge_base(b)
    build_merge_revision(r)
    build_merge_current(c)
    resp = _post(client, _files(tmp_path, b.read_bytes(),
                                r.read_bytes(), c.read_bytes()))
    assert resp.status_code == 202, resp.text
    job = _wait(client, resp.json()["id"])
    assert job["status"] == "done"
    assert job["kind"] == "merge"
    assert job["merge_ok"] is True
    assert job["conflicts"] == [] and job["violations"] == []
    kinds = {(ch["table"], ch["pk"], ch["kind"]) for ch in job["changes"]}
    assert ("dept", 2, "update") in kinds
    assert ("employee", 12, "delete") in kinds
    assert job["download_available"] is True

    dl = client.get(f"/api/merge-jobs/{job['id']}/download")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/x-sqlite3"
    out = tmp_path / "downloaded.db"
    out.write_bytes(dl.content)
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # 修订生效 + 当前补录保留
        assert conn.execute("SELECT name FROM dept WHERE id=2").fetchone()[0] == "QA-Renamed"
        assert conn.execute("SELECT name FROM dept WHERE id=5").fetchone()[0] == "Ops"
        assert conn.execute("SELECT name FROM employee WHERE id=14").fetchone()[0] == "Erin"
        assert conn.execute("SELECT COUNT(*) FROM employee WHERE id=12").fetchone()[0] == 0
    finally:
        conn.close()


def test_merge_conflict_no_download(client, tmp_path):
    base = _db_bytes(tmp_path, "b.db", [(1, None, "A")], [(10, 1, "X")])
    revision = _db_bytes(tmp_path, "r.db", [(1, None, "A2")], [(10, 1, "X")])
    current = _db_bytes(tmp_path, "c.db", [(1, None, "A3")], [(10, 1, "X")])
    resp = _post(client, _files(tmp_path, base, revision, current))
    job = _wait(client, resp.json()["id"])
    assert job["status"] == "done"
    assert job["merge_ok"] is False
    assert len(job["conflicts"]) == 1
    assert job["download_available"] is False
    dl = client.get(f"/api/merge-jobs/{job['id']}/download")
    assert dl.status_code == 400


def test_merge_fk_violation_no_partial_result(client, tmp_path):
    base = _db_bytes(
        tmp_path, "b.db",
        [(1, 10, "A"), (2, 12, "B")],
        [(10, 1, "X"), (12, 2, "Z")],
    )
    revision = _db_bytes(
        tmp_path, "r.db",
        [(1, 10, "A"), (2, None, "B")],
        [(10, 1, "X")],
    )
    current = _db_bytes(
        tmp_path, "c.db",
        [(1, 10, "A"), (2, 12, "B"), (5, 12, "New")],
        [(10, 1, "X"), (12, 2, "Z")],
    )
    resp = _post(client, _files(tmp_path, base, revision, current))
    job = _wait(client, resp.json()["id"])
    assert job["merge_ok"] is False
    assert job["conflicts"] == []
    assert job["violations"] and job["violations"][0]["missing_pk"] == 12
    assert job["download_available"] is False


def test_merge_rejects_non_sqlite(client, tmp_path):
    good = _db_bytes(tmp_path, "g.db", [(1, None, "A")], [(10, 1, "X")])
    resp = _post(client, {
        "base_db": ("base.db", b"not a database", "application/octet-stream"),
        "revision_db": ("r.db", good, "application/x-sqlite3"),
        "current_db": ("c.db", good, "application/x-sqlite3"),
    })
    assert resp.status_code == 400
    assert "SQLite" in resp.json()["detail"]


def test_merge_rejects_schema_violation(client, tmp_path):
    # 当前库本身外键失效 -> 校验阶段失败，任务 error，不产出
    base = _db_bytes(tmp_path, "b.db", [(1, None, "A")], [(10, 1, "X")])
    rev = _db_bytes(tmp_path, "r.db", [(1, None, "A")], [(10, 1, "X")])
    bad_path = tmp_path / "bad.db"
    conn = sqlite3.connect(str(bad_path))
    conn.executescript(
        """
        CREATE TABLE dept (id INTEGER PRIMARY KEY, leader_id INTEGER, name TEXT NOT NULL);
        CREATE TABLE employee (id INTEGER PRIMARY KEY,
                               dept_id INTEGER NOT NULL REFERENCES dept(id), name TEXT NOT NULL);
        INSERT INTO dept VALUES (1, 99, 'A');
        INSERT INTO employee VALUES (10, 1, 'X');
        """
    )
    conn.commit()
    conn.close()
    resp = _post(client, _files(tmp_path, base, rev, bad_path.read_bytes()))
    job = _wait(client, resp.json()["id"])
    assert job["status"] == "error"
    assert "外键" in job["message"]


def test_merge_uploads_remain_readonly_and_intact(client, tmp_path):
    b, r, c = tmp_path / "b.db", tmp_path / "r.db", tmp_path / "c.db"
    build_merge_base(b)
    build_merge_revision(r)
    build_merge_current(c)
    import hashlib

    digests = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (b, r, c)}
    resp = _post(client, _files(tmp_path, b.read_bytes(), r.read_bytes(),
                                c.read_bytes()))
    job = _wait(client, resp.json()["id"])
    assert job["merge_ok"] is True
    # 任务目录里的上传原件仍是只读且内容不变
    job_dir = DATA_DIR / "jobs" / job["id"]
    for key, src in (("base", b), ("revision", r), ("current", c)):
        upload = job_dir / f"upload_{key}.db"
        assert oct(upload.stat().st_mode & 0o777) == "0o444"
        assert hashlib.sha256(upload.read_bytes()).hexdigest() == digests[src]


def test_merge_sample_static_files_served(client):
    r = client.get("/api/samples")
    info = r.json()["merge_samples"]["merge_demo"]
    for role in ("base_url", "revision_url", "current_url"):
        assert client.get(info[role]).status_code == 200
