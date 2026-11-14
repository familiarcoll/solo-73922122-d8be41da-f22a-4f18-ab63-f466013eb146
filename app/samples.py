"""内置样例：循环外键、先提交后失败、三方合并。

样例文件同时通过 /static 目录提供下载，便于用户直接体验。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CYCLIC_SQL = """\
-- 三表循环外键：dept <-> employee <-> project
-- 原始库中所有外键均有效。下面的语句必然触发唯一约束错误。
INSERT INTO dept (id, name) VALUES (1, 'Platform');
"""

COMMIT_THEN_FAIL_SQL = """\
-- 先提交修改，再触发 NOT NULL 约束错误。
-- 删除的是叶子节点（无子节点引用），COMMIT 必然成功；
-- 随后的 INSERT 因 label 为 NULL 失败。
-- 每个试跑使用独立副本，已提交的 DELETE 不会污染原件或后续试跑。
BEGIN;
DELETE FROM node WHERE id = 3;
COMMIT;
INSERT INTO node (id, parent_id, label) VALUES (9, 1, NULL);
"""


def build_cyclic(path: str | Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE dept (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE employee (
                id INTEGER PRIMARY KEY,
                dept_id INTEGER NOT NULL REFERENCES dept(id),
                mentor_id INTEGER REFERENCES employee(id)
            );
            CREATE TABLE project (
                id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL REFERENCES employee(id),
                dept_id INTEGER NOT NULL REFERENCES dept(id)
            );
            """
        )
        conn.executescript(
            """
            INSERT INTO dept (id, name) VALUES
                (1, 'Platform'), (2, 'QA'), (3, 'Sales');
            INSERT INTO employee (id, dept_id, mentor_id) VALUES
                (10, 1, NULL), (11, 1, 10), (12, 2, 10);
            -- 循环外键：项目属于部门，又由部门内员工负责
            INSERT INTO project (id, owner_id, dept_id) VALUES
                (100, 10, 1), (101, 11, 2), (102, 12, 3);
            """
        )
        conn.commit()
    finally:
        conn.close()


def build_commit_then_fail(path: str | Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE node (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER REFERENCES node(id),
                label TEXT NOT NULL
            );
            """
        )
        conn.executescript(
            """
            INSERT INTO node (id, parent_id, label) VALUES
                (1, NULL, 'root'),
                (2, 1, 'child-a'),
                (3, 2, 'child-b');
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 三方合并样例：两张表互相引用（循环外键 dept.leader_id <-> employee.dept_id）。
# 基准 -> 修订包含 增 / 删 / 改 三种变更；当前库在同一结构上追加补录，
# 且与修订不碰同一行，因此可以无冲突合并并保持外键有效。
# ---------------------------------------------------------------------------
MERGE_SCHEMA = """\
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


def _build_merge_db(
    path: str | Path,
    dept_rows: list[tuple],
    employee_rows: list[tuple],
) -> None:
    conn = sqlite3.connect(str(path))
    try:
        # 建表含双向循环外键；插入阶段临时关闭强制，插完再整体校验
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(MERGE_SCHEMA)
        conn.executemany(
            "INSERT INTO dept (id, leader_id, name) VALUES (?, ?, ?)", dept_rows
        )
        conn.executemany(
            "INSERT INTO employee (id, dept_id, name) VALUES (?, ?, ?)",
            employee_rows,
        )
        conn.commit()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def build_merge_base(path: str | Path) -> None:
    _build_merge_db(
        path,
        dept_rows=[
            (1, 10, "Platform"),
            (2, 11, "QA"),
            (3, 12, "Sales"),
        ],
        employee_rows=[
            (10, 1, "Alice"),
            (11, 2, "Bob"),
            (12, 3, "Carol"),
        ],
    )


def build_merge_revision(path: str | Path) -> None:
    # 相对基准：UPDATE dept 2 名称；DELETE employee 12（dept 3 的 leader 同步置空）；
    # INSERT dept 4 + employee 13
    _build_merge_db(
        path,
        dept_rows=[
            (1, 10, "Platform"),
            (2, 11, "QA-Renamed"),       # UPDATE
            (3, None, "Sales"),           # leader 12 随删除被清空
            (4, 13, "Support"),           # INSERT
        ],
        employee_rows=[
            (10, 1, "Alice"),
            (11, 2, "Bob"),
            # employee 12 Carol 被 DELETE
            (13, 4, "Dave"),              # INSERT
        ],
    )


def build_merge_current(path: str | Path) -> None:
    # 相对基准只做补录，不碰修订改动的行 -> 无冲突；
    # 新增 dept 5 + employee 14，合并后必须仍然保留。
    _build_merge_db(
        path,
        dept_rows=[
            (1, 10, "Platform"),
            (2, 11, "QA"),
            (3, 12, "Sales"),
            (5, 14, "Ops"),               # 当前库补录
        ],
        employee_rows=[
            (10, 1, "Alice"),
            (11, 2, "Bob"),
            (12, 3, "Carol"),
            (14, 5, "Erin"),              # 当前库补录
        ],
    )


SAMPLES = {
    "cyclic": {
        "title": "循环外键（三表）+ 唯一约束错误",
        "db_builder": build_cyclic,
        "script": CYCLIC_SQL,
        "db_filename": "sample_cyclic.db",
        "sql_filename": "sample_cyclic.sql",
    },
    "commit_then_fail": {
        "title": "先提交后失败（自引用循环外键）",
        "db_builder": build_commit_then_fail,
        "script": COMMIT_THEN_FAIL_SQL,
        "db_filename": "sample_commit_then_fail.db",
        "sql_filename": "sample_commit_then_fail.sql",
    },
}

# 三方合并样例：三个结构相同的库
MERGE_SAMPLES = {
    "merge_demo": {
        "title": "三方合并样例（循环外键；修订含增删改，当前库含补录）",
        "builders": {
            "base": build_merge_base,
            "revision": build_merge_revision,
            "current": build_merge_current,
        },
        "filenames": {
            "base": "sample_merge_base.db",
            "revision": "sample_merge_revision.db",
            "current": "sample_merge_current.db",
        },
    },
}


def ensure_samples(static_dir: Path) -> dict[str, dict[str, str]]:
    """在静态目录中生成样例文件，返回下载路径映射。"""
    static_dir.mkdir(parents=True, exist_ok=True)
    info: dict[str, dict[str, str]] = {}
    for key, spec in SAMPLES.items():
        db_path = static_dir / spec["db_filename"]
        sql_path = static_dir / spec["sql_filename"]
        if not db_path.exists():
            spec["db_builder"](db_path)
        if not sql_path.exists():
            sql_path.write_text(spec["script"], encoding="utf-8")
        info[key] = {
            "title": spec["title"],
            "db_url": f"/static/{spec['db_filename']}",
            "sql_url": f"/static/{spec['sql_filename']}",
        }

    merge_info: dict[str, dict[str, str]] = {}
    for key, spec in MERGE_SAMPLES.items():
        urls: dict[str, str] = {}
        for role in ("base", "revision", "current"):
            filename = spec["filenames"][role]
            db_path = static_dir / filename
            if not db_path.exists():
                spec["builders"][role](db_path)
            urls[f"{role}_url"] = f"/static/{filename}"
        merge_info[key] = {"title": spec["title"], **urls}
    info["merge_samples"] = merge_info
    return info
