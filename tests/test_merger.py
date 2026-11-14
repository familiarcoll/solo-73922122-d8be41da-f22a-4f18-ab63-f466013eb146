"""三方合并核心逻辑单元测试。"""
from __future__ import annotations

import sqlite3

import pytest

from app.errors import ValidationError
from app.merger import (
    three_way_merge,
    write_merged_database,
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


def _make_db(path, dept_rows, employee_rows):
    """建含双向循环外键的库；插数据时临时关闭外键强制，插完校验。"""
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
    return path


@pytest.fixture()
def trio(tmp_path):
    base = _make_db(
        tmp_path / "base.db",
        [(1, 10, "Platform"), (2, 11, "QA"), (3, 12, "Sales")],
        [(10, 1, "Alice"), (11, 2, "Bob"), (12, 3, "Carol")],
    )
    revision = _make_db(
        tmp_path / "revision.db",
        [(1, 10, "Platform"), (2, 11, "QA2"), (3, None, "Sales")],
        [(10, 1, "Alice"), (11, 2, "Bob")],  # employee 12 删除
    )
    current = _make_db(
        tmp_path / "current.db",
        [(1, 10, "Platform"), (2, 11, "QA"), (3, 12, "Sales"), (4, 13, "Ops")],
        [(10, 1, "Alice"), (11, 2, "Bob"), (12, 3, "Carol"), (13, 4, "Dan")],
    )
    return base, revision, current


def test_clean_merge_applies_changes_and_keeps_supplements(trio, tmp_path):
    base, revision, current = trio
    result = three_way_merge(str(base), str(revision), str(current))
    assert result.ok
    kinds = sorted((c.table, c.pk, c.kind) for c in result.changes)
    assert ("dept", 2, "update") in kinds
    assert ("dept", 3, "update") in kinds  # leader 12 -> NULL
    assert ("employee", 12, "delete") in kinds
    # 当前库补录 dept 4 / employee 13 不是修订变更
    assert not any(c.pk in (4, 13) for c in result.changes)
    assert result.conflicts == [] and result.violations == []

    out = tmp_path / "merged.db"
    write_merged_database(str(current), result, str(out))
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # 修订生效
        assert conn.execute("SELECT name FROM dept WHERE id=2").fetchone()[0] == "QA2"
        assert conn.execute("SELECT leader_id FROM dept WHERE id=3").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM employee WHERE id=12").fetchone()[0] == 0
        # 当前库补录保留
        assert conn.execute("SELECT name FROM dept WHERE id=4").fetchone()[0] == "Ops"
        assert conn.execute("SELECT name FROM employee WHERE id=13").fetchone()[0] == "Dan"
        # 行数
        assert conn.execute("SELECT COUNT(*) FROM dept").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM employee").fetchone()[0] == 3
    finally:
        conn.close()


def test_identical_databases_merge_without_changes(tmp_path):
    rows_d = [(1, 10, "A")]
    rows_e = [(10, 1, "X")]
    paths = [_make_db(tmp_path / f"{n}.db", rows_d, rows_e)
             for n in ("b", "r", "c")]
    result = three_way_merge(*map(str, paths))
    assert result.ok
    assert result.changes == []
    assert result.conflicts == [] and result.violations == []


def test_update_update_conflict(tmp_path):
    base = _make_db(tmp_path / "b.db", [(1, 10, "A")], [(10, 1, "X")])
    revision = _make_db(tmp_path / "r.db", [(1, 10, "A2")], [(10, 1, "X")])
    current = _make_db(tmp_path / "c.db", [(1, 10, "A3")], [(10, 1, "X")])
    result = three_way_merge(str(base), str(revision), str(current))
    assert not result.ok
    assert len(result.conflicts) == 1
    cf = result.conflicts[0]
    assert cf.table == "dept" and cf.pk == 1
    assert "修改" in cf.reason
    assert result.merged_rows == {}  # 冲突时不产出合并数据
    assert result.failure_reason and "冲突" in result.failure_reason


def test_update_vs_delete_conflict(tmp_path):
    base = _make_db(tmp_path / "b.db", [(1, 10, "A")], [(10, 1, "X")])
    revision = _make_db(tmp_path / "r.db", [(1, 10, "A2")], [(10, 1, "X")])
    current = _make_db(tmp_path / "c.db", [], [])  # 当前库删光
    result = three_way_merge(str(base), str(revision), str(current))
    assert not result.ok
    reasons = {(cf.table, cf.pk): cf.reason for cf in result.conflicts}
    assert any("已删除" in r for r in reasons.values())


def test_delete_vs_update_conflict(tmp_path):
    base = _make_db(tmp_path / "b.db", [(1, 10, "A")], [(10, 1, "X")])
    revision = _make_db(tmp_path / "r.db", [], [])  # 修订删光
    current = _make_db(tmp_path / "c.db", [(1, 10, "A9")], [(10, 1, "X")])
    result = three_way_merge(str(base), str(revision), str(current))
    assert not result.ok
    assert any("删除了该行" in cf.reason for cf in result.conflicts)


def test_dual_insert_same_pk_different_value_conflict(tmp_path):
    # 基准没有 dept 5，修订和当前都插入 id=5 但内容不同
    base = _make_db(tmp_path / "b.db", [(1, 10, "A")], [(10, 1, "X")])
    revision = _make_db(tmp_path / "r.db",
                        [(1, 10, "A"), (5, None, "R")], [(10, 1, "X")])
    current = _make_db(tmp_path / "c.db",
                       [(1, 10, "A"), (5, None, "C")], [(10, 1, "X")])
    result = three_way_merge(str(base), str(revision), str(current))
    assert not result.ok
    assert any(cf.table == "dept" and cf.pk == 5 for cf in result.conflicts)


def test_dual_insert_same_value_is_idempotent_no_conflict(tmp_path):
    base = _make_db(tmp_path / "b.db", [(1, 10, "A")], [(10, 1, "X")])
    same = [(1, 10, "A"), (5, None, "N")]
    revision = _make_db(tmp_path / "r.db", same, [(10, 1, "X")])
    current = _make_db(tmp_path / "c.db", same, [(10, 1, "X")])
    result = three_way_merge(str(base), str(revision), str(current))
    assert result.ok
    assert result.conflicts == []


def test_compatible_changes_on_different_rows_no_conflict(tmp_path):
    base = _make_db(tmp_path / "b.db",
                    [(1, 10, "A"), (2, 11, "B")],
                    [(10, 1, "X"), (11, 2, "Y")])
    # 修订改 dept 1，当前改 dept 2：不同行，无冲突
    revision = _make_db(tmp_path / "r.db",
                        [(1, 10, "A2"), (2, 11, "B")],
                        [(10, 1, "X"), (11, 2, "Y")])
    current = _make_db(tmp_path / "c.db",
                       [(1, 10, "A"), (2, 11, "B2")],
                       [(10, 1, "X"), (11, 2, "Y")])
    result = three_way_merge(str(base), str(revision), str(current))
    assert result.ok
    assert result.conflicts == []
    merged_dept = {pk: v for pk, v in result.merged_rows["dept"].items()}
    # 合并后两行的修改都在
    assert merged_dept[1][2] == "A2"
    assert merged_dept[2][2] == "B2"


def test_foreign_key_violation_after_merge_reported(tmp_path):
    # 每个库单独都外键有效；但当前库的“新补录”引用了修订库删除的行，
    # 合并后才出现孤儿引用——即使没有同行冲突也要指出并拒绝产出。
    base = _make_db(
        tmp_path / "b.db",
        [(1, 10, "A"), (2, 12, "B")],
        [(10, 1, "X"), (11, 1, "Y"), (12, 2, "Z")],
    )
    # 修订：删除 employee 12，并把引用它的 dept 2 的 leader 置空（库内自洽）
    revision = _make_db(
        tmp_path / "r.db",
        [(1, 10, "A"), (2, None, "B")],
        [(10, 1, "X"), (11, 1, "Y")],
    )
    # 当前：不动修订改的行，只补录 dept 5，其 leader 指向 employee 12（当前库内有效）
    current = _make_db(
        tmp_path / "c.db",
        [(1, 10, "A"), (2, 12, "B"), (5, 12, "New")],
        [(10, 1, "X"), (11, 1, "Y"), (12, 2, "Z")],
    )
    result = three_way_merge(str(base), str(revision), str(current))
    assert not result.ok
    assert result.conflicts == []  # 没有同行冲突
    assert len(result.violations) >= 1
    v = result.violations[0]
    assert v.table == "dept" and v.pk == 5 and v.column == "leader_id"
    assert v.parent_table == "employee" and v.missing_pk == 12
    assert result.failure_reason and "外键" in result.failure_reason


def test_no_merged_file_on_conflict(trio, tmp_path):
    base, revision, current = trio
    # 制造冲突：当前库也改 dept 2
    conn = sqlite3.connect(str(current))
    conn.execute("UPDATE dept SET name='QA-current' WHERE id=2")
    conn.commit()
    conn.close()
    result = three_way_merge(str(base), str(revision), str(current))
    assert not result.ok and result.conflicts
    out = tmp_path / "should-not-exist.db"
    with pytest.raises(ValidationError):
        write_merged_database(str(current), result, str(out))
    assert not out.exists()


def test_schema_mismatch_rejected(tmp_path):
    base = _make_db(tmp_path / "b.db", [(1, 10, "A")], [(10, 1, "X")])
    revision = _make_db(tmp_path / "r.db", [(1, 10, "A")], [(10, 1, "X")])
    # 当前库多一张列
    other = tmp_path / "c.db"
    conn = sqlite3.connect(str(other))
    conn.executescript(
        """
        CREATE TABLE dept (id INTEGER PRIMARY KEY, leader_id INTEGER,
                           name TEXT NOT NULL, extra TEXT);
        CREATE TABLE employee (id INTEGER PRIMARY KEY,
                               dept_id INTEGER NOT NULL REFERENCES dept(id),
                               name TEXT NOT NULL);
        INSERT INTO dept VALUES (1, NULL, 'A', 'x');
        INSERT INTO employee VALUES (10, 1, 'X');
        """
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValidationError, match="列结构不一致"):
        three_way_merge(str(base), str(revision), str(other))


def test_upload_files_untouched_after_failed_merge(tmp_path):
    base = _make_db(tmp_path / "b.db", [(1, 10, "A")], [(10, 1, "X")])
    revision = _make_db(tmp_path / "r.db", [], [])
    current = _make_db(tmp_path / "c.db", [(1, 10, "A9")], [(10, 1, "X")])
    import hashlib

    digests = {p: hashlib.sha256(p.read_bytes()).hexdigest()
               for p in (base, revision, current)}
    result = three_way_merge(str(base), str(revision), str(current))
    assert not result.ok
    for p, digest in digests.items():
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest


def test_table_set_mismatch_rejected(tmp_path):
    def build(path, with_x):
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE a (id INTEGER PRIMARY KEY);
            CREATE TABLE b (id INTEGER PRIMARY KEY,
                            a_id INTEGER NOT NULL REFERENCES a(id));
            """
        )
        if with_x:
            conn.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO x VALUES (7)")
        conn.execute("INSERT INTO a VALUES (1)")
        conn.execute("INSERT INTO b VALUES (10, 1)")
        conn.commit()
        conn.close()
        return path

    b = build(tmp_path / "b.db", False)
    r = build(tmp_path / "r.db", False)
    c = build(tmp_path / "c.db", True)
    with pytest.raises(ValidationError, match="表集合不一致"):
        three_way_merge(str(b), str(r), str(c))


def test_self_referential_cycle_merge(tmp_path):
    """自引用循环外键下的增删改合并。"""
    def build(path, rows):
        conn = sqlite3.connect(str(path))
        conn.executescript(
            "CREATE TABLE node (id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES node(id), label TEXT NOT NULL);"
        )
        conn.executemany("INSERT INTO node VALUES (?, ?, ?)", rows)
        conn.commit()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()
        return path

    base = build(tmp_path / "b.db",
                 [(1, None, "r"), (2, 1, "a"), (3, 2, "b")])
    revision = build(tmp_path / "r.db",
                     [(1, None, "r"), (2, 1, "a2"), (4, 1, "c")])  # 改2、删3、增4
    current = build(tmp_path / "c.db",
                    [(1, None, "r"), (2, 1, "a"), (3, 2, "b"), (5, 1, "d")])
    result = three_way_merge(str(base), str(revision), str(current))
    assert result.ok
    out = tmp_path / "m.db"
    write_merged_database(str(current), result, str(out))
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        labels = dict(conn.execute("SELECT id, label FROM node").fetchall())
        assert labels == {1: "r", 2: "a2", 4: "c", 5: "d"}  # 3 被删，补录 5 保留
    finally:
        conn.close()
