"""三方合并（three-way merge）。

入仓开发者修正“暂存数据”期间，入仓库可能又收到新的补录。直接整库覆盖会丢掉
补录，因此需要以**基准库**（共同祖先）为参照：

1. 按表 + 单列整数主键，找出基准 -> 修订的增 / 删 / 改；
2. 只把这些变更应用到**当前库副本**，当前库的其它补录原样保留；
3. 当前库对同一行也做了不兼容变更时报告**行级冲突**，不产出任何部分结果；
4. 即便没有同一行冲突，应用后最终外键失效时，指出所有涉及行，同样拒绝产出；
5. 允许循环外键：应用阶段关闭 foreign_keys，最终用
   ``PRAGMA foreign_key_check`` 做整体校验。

三个上传文件本身从不被写入，任何失败都不产生合并库。
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ValidationError
from .validation import FkEdge, RowValues, Schema, validate_database


@dataclass
class RowChange:
    """基准 -> 修订之间的一行变更。"""

    table: str
    pk: int
    kind: str  # insert / update / delete
    before: RowValues | None = None
    after: RowValues | None = None

    def to_dict(self, columns: dict[str, list[str]]) -> dict[str, object]:
        return {
            "table": self.table,
            "pk": self.pk,
            "kind": self.kind,
            "columns": columns[self.table],
            "before": _jsonable(self.before),
            "after": _jsonable(self.after),
        }


@dataclass
class MergeConflict:
    """同一主键的行在修订库与当前库上发生不兼容变更。"""

    table: str
    pk: int
    reason: str
    base: RowValues | None
    revision: RowValues | None
    current: RowValues | None

    def to_dict(self, columns: dict[str, list[str]]) -> dict[str, object]:
        return {
            "table": self.table,
            "pk": self.pk,
            "reason": self.reason,
            "columns": columns[self.table],
            "base": _jsonable(self.base),
            "revision": _jsonable(self.revision),
            "current": _jsonable(self.current),
        }


@dataclass
class FkViolation:
    """合并结果中失效的外键引用：某表某行的某列指向了不存在的父表主键。"""

    table: str
    pk: int
    column: str
    parent_table: str
    missing_pk: int

    def to_dict(self) -> dict[str, object]:
        return {
            "table": self.table,
            "pk": self.pk,
            "column": self.column,
            "parent_table": self.parent_table,
            "missing_pk": self.missing_pk,
        }


@dataclass
class MergeResult:
    ok: bool
    tables: list[str]
    columns: dict[str, list[str]]
    pk_column: dict[str, str]
    fk_edges: list[FkEdge]
    changes: list[RowChange] = field(default_factory=list)
    conflicts: list[MergeConflict] = field(default_factory=list)
    violations: list[FkViolation] = field(default_factory=list)
    row_counts_base: dict[str, int] = field(default_factory=dict)
    row_counts_revision: dict[str, int] = field(default_factory=dict)
    row_counts_current: dict[str, int] = field(default_factory=dict)
    row_counts_merged: dict[str, int] = field(default_factory=dict)
    merged_rows: dict[str, dict[int, RowValues]] = field(default_factory=dict)
    failure_reason: str | None = None


def _jsonable(values: RowValues | None) -> list[object] | None:
    if values is None:
        return None
    out: list[object] = []
    for value in values:
        if isinstance(value, (str, int, float, bool)) or value is None:
            out.append(value)
        else:  # bytes 等非常规类型，转成可 JSON 展示的字符串
            out.append(repr(value))
    return out


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [col["name"] for col in conn.execute(f'PRAGMA table_info("{table}")')]


def _load_snapshot(
    db_path: str, label: str
) -> tuple[Schema, dict[str, list[str]], dict[str, dict[int, RowValues]]]:
    """校验单个库并加载 (表, 主键) -> 整行值 的快照。"""
    schema = validate_database(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns: dict[str, list[str]] = {}
        snapshot: dict[str, dict[int, RowValues]] = {}
        for table in schema.tables:
            cols = _table_columns(conn, table)
            columns[table] = cols
            quoted = ", ".join(f'"{name}"' for name in cols)
            pk_col = schema.pk_column[table]
            pk_index = cols.index(pk_col)
            rows: dict[int, RowValues] = {}
            for row in conn.execute(
                f'SELECT {quoted} FROM "{table}" ORDER BY "{pk_col}"'
            ):
                values = tuple(row[name] for name in cols)
                pk_value = values[pk_index]
                if not isinstance(pk_value, int) or isinstance(pk_value, bool):
                    raise ValidationError(
                        f"{label} 表 {table} 存在非整数主键值 {pk_value!r}。"
                    )
                rows[int(pk_value)] = values
            snapshot[table] = rows
    finally:
        conn.close()
    return schema, columns, snapshot


def _ensure_same_schema(
    base: Schema,
    revision: Schema,
    current: Schema,
    base_cols: dict[str, list[str]],
    rev_cols: dict[str, list[str]],
    cur_cols: dict[str, list[str]],
) -> None:
    if not (base.tables == revision.tables == current.tables):
        raise ValidationError(
            "三个库的表集合不一致，无法合并："
            f"基准={base.tables}，修订={revision.tables}，当前={current.tables}。"
        )
    for table in base.tables:
        if not (
            base.pk_column[table]
            == revision.pk_column[table]
            == current.pk_column[table]
        ):
            raise ValidationError(f"表 {table} 的主键列在三个库中不一致。")
        if not (base_cols[table] == rev_cols[table] == cur_cols[table]):
            raise ValidationError(
                f"表 {table} 的列结构不一致，无法按主键合并："
                f"基准={base_cols[table]}，修订={rev_cols[table]}，"
                f"当前={cur_cols[table]}。"
            )
    if not (
        sorted(base.fk_edges) == sorted(revision.fk_edges)
        and sorted(base.fk_edges) == sorted(current.fk_edges)
    ):
        raise ValidationError("三个库的外键结构不一致，拒绝合并。")


def _diff_and_conflicts(
    tables: list[str],
    base: dict[str, dict[int, RowValues]],
    revision: dict[str, dict[int, RowValues]],
    current: dict[str, dict[int, RowValues]],
) -> tuple[list[RowChange], list[MergeConflict]]:
    """按表和主键计算基准->修订的变更，并检测与当前库的同行冲突。"""
    changes: list[RowChange] = []
    conflicts: list[MergeConflict] = []
    for table in tables:
        b_rows, r_rows, c_rows = base[table], revision[table], current[table]
        all_pks = sorted(set(b_rows) | set(r_rows) | set(c_rows))
        for pk in all_pks:
            bv, rv, cv = b_rows.get(pk), r_rows.get(pk), c_rows.get(pk)
            in_b, in_r, in_c = bv is not None, rv is not None, cv is not None

            if in_b and in_r and bv != rv:
                # 基准 -> 修订：UPDATE
                changes.append(RowChange(table, pk, "update", bv, rv))
                if in_c and cv != bv and cv != rv:
                    # 当前库也改了同一行，且与两边都不同
                    conflicts.append(
                        MergeConflict(
                            table, pk,
                            "修订库与当前库都修改了同一行，且修改结果不一致。",
                            bv, rv, cv,
                        )
                    )
                elif not in_c:
                    # 当前库已删除该行，而修订库要修改它
                    conflicts.append(
                        MergeConflict(
                            table, pk,
                            "修订库修改了该行，但当前库已删除该行。",
                            bv, rv, None,
                        )
                    )
            elif not in_b and in_r:
                # 基准 -> 修订：INSERT
                changes.append(RowChange(table, pk, "insert", None, rv))
                if in_c and cv != rv:
                    # 当前库也独立插入了同一主键但内容不同
                    conflicts.append(
                        MergeConflict(
                            table, pk,
                            "修订库与当前库都新增了同一主键的行，但数据不一致。",
                            None, rv, cv,
                        )
                    )
            elif in_b and not in_r:
                # 基准 -> 修订：DELETE
                changes.append(RowChange(table, pk, "delete", bv, None))
                if in_c and cv != bv:
                    # 当前库修改了修订库想删除的行
                    conflicts.append(
                        MergeConflict(
                            table, pk,
                            "修订库删除了该行，但当前库修改了同一行。",
                            bv, None, cv,
                        )
                    )
            # 基准与修订一致（含两边都不存在）的行不属于“修订变更”，
            # 当前库的补录在应用阶段自然保留，不产生冲突。
    return changes, conflicts


def three_way_merge(
    base_path: str, revision_path: str, current_path: str
) -> MergeResult:
    """计算三方合并；只在无冲突且最终外键有效时标记 ok=True。"""
    base_schema, base_cols, base_rows = _load_snapshot(base_path, "基准库")
    rev_schema, rev_cols, rev_rows = _load_snapshot(revision_path, "修订库")
    cur_schema, cur_cols, cur_rows = _load_snapshot(current_path, "当前库")
    _ensure_same_schema(
        base_schema, rev_schema, cur_schema, base_cols, rev_cols, cur_cols
    )
    tables = base_schema.tables

    changes, conflicts = _diff_and_conflicts(tables, base_rows, rev_rows, cur_rows)

    result = MergeResult(
        ok=False,
        tables=tables,
        columns=base_cols,
        pk_column=base_schema.pk_column,
        fk_edges=base_schema.fk_edges,
        changes=changes,
        conflicts=conflicts,
        row_counts_base={t: len(base_rows[t]) for t in tables},
        row_counts_revision={t: len(rev_rows[t]) for t in tables},
        row_counts_current={t: len(cur_rows[t]) for t in tables},
    )

    if conflicts:
        result.failure_reason = (
            f"检测到 {len(conflicts)} 处同一行冲突，已拒绝合并，未生成任何合并库。"
        )
        return result

    # 无冲突：以当前库为底，只应用基准 -> 修订的变更，其余补录保留。
    merged: dict[str, dict[int, RowValues]] = {
        t: dict(cur_rows[t]) for t in tables
    }
    for change in changes:
        if change.kind == "insert":
            # 当前库同主键同内容时保持幂等；同主键不同内容已在冲突阶段拦截
            merged[change.table][change.pk] = change.after  # type: ignore[assignment]
        elif change.kind == "update":
            merged[change.table][change.pk] = change.after  # type: ignore[assignment]
        elif change.kind == "delete":
            merged[change.table].pop(change.pk, None)
    result.merged_rows = merged
    result.row_counts_merged = {t: len(merged[t]) for t in tables}

    violations = _check_merged_foreign_keys(merged, base_schema, base_cols)
    result.violations = violations
    if violations:
        result.failure_reason = (
            f"应用修订后有 {len(violations)} 处外键引用失效，"
            "已拒绝合并，未生成任何合并库。"
        )
        return result

    result.ok = True
    return result


def _check_merged_foreign_keys(
    merged: dict[str, dict[int, RowValues]],
    schema: Schema,
    columns: dict[str, list[str]],
) -> list[FkViolation]:
    """在合并后的行集合上逐条检查外键，返回全部失效引用。

    循环外键天然支持：只判断“被引用主键行是否存在”，不做删除顺序假设。
    """
    violations: list[FkViolation] = []
    for src_table, src_col, dst_table, _dst_col in schema.fk_edges:
        col_index = columns[src_table].index(src_col)
        pk_index = columns[src_table].index(schema.pk_column[src_table])
        target_pks = merged[dst_table].keys()
        for values in merged[src_table].values():
            fk_value = values[col_index]
            if fk_value is None:
                continue  # 可空外键的 NULL 不构成引用
            if not isinstance(fk_value, int) or isinstance(fk_value, bool):
                violations.append(
                    FkViolation(
                        table=src_table,
                        pk=int(values[pk_index]),
                        column=src_col,
                        parent_table=dst_table,
                        missing_pk=-1,
                    )
                )
                continue
            if int(fk_value) not in target_pks:
                violations.append(
                    FkViolation(
                        table=src_table,
                        pk=int(values[pk_index]),
                        column=src_col,
                        parent_table=dst_table,
                        missing_pk=int(fk_value),
                    )
                )
    violations.sort(key=lambda v: (v.table, v.pk, v.column))
    return violations


def write_merged_database(
    current_db_path: str, result: MergeResult, out_path: str
) -> None:
    """把合并结果写入当前库的副本，并再次做 SQLite 级外键/完整性校验。

    只在 ``result.ok`` 时调用；应用阶段关闭外键强制以允许循环外键，
    写入完成后用 ``PRAGMA foreign_key_check`` 复核。
    """
    if not result.ok:
        raise ValidationError("存在冲突或外键失效，不能写出合并库。")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(current_db_path, out_path)
    conn = sqlite3.connect(out_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in result.tables:
            cols = result.columns[table]
            quoted_cols = ", ".join(f'"{name}"' for name in cols)
            placeholders = ", ".join("?" for _ in cols)
            # 先清空再按合并快照重写，保证磁盘结果与内存计算完全一致
            conn.execute(f'DELETE FROM "{table}"')
            rows = [
                result.merged_rows[table][pk]
                for pk in sorted(result.merged_rows[table])
            ]
            conn.executemany(
                f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders})',
                rows,
            )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        raw_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if raw_violations:
            detail = ", ".join(map(str, raw_violations[:10]))
            raise ValidationError(f"合并库外键校验失败（内部错误）：{detail}")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValidationError(f"合并库完整性检查失败：{integrity}")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(out_path + suffix).unlink()
        except FileNotFoundError:
            pass
