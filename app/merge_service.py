"""三方合并任务流水线：校验上传 -> 计算合并 -> 写出合并库。

与缩减流水线一样，上传原件落盘后设为只读；合并计算全程只读三个原件，
只有成功时才在任务目录写出 merged.db。冲突或外键失效时不产出任何部分结果。
"""
from __future__ import annotations

from pathlib import Path

from .jobs import Job
from .merger import MergeResult, three_way_merge, write_merged_database
from .runner import make_canonical_copy


def _public_changes(result: MergeResult) -> list[dict[str, object]]:
    return [change.to_dict(result.columns) for change in result.changes]


def _public_conflicts(result: MergeResult) -> list[dict[str, object]]:
    return [conflict.to_dict(result.columns) for conflict in result.conflicts]


def run_merge_pipeline(
    job: Job,
    base_db: str,
    revision_db: str,
    current_db: str,
) -> None:
    job.status = "running"
    job.message = "正在校验三个数据库的结构与外键…"
    job_dir = Path(job.dir)

    # 规范副本：统一日志模式并做完整性检查，原件保持只读不动
    canonical_paths = {}
    for label, upload in (
        ("base", base_db),
        ("revision", revision_db),
        ("current", current_db),
    ):
        canonical = job_dir / f"canonical_{label}.db"
        make_canonical_copy(upload, str(canonical))
        canonical_paths[label] = str(canonical)

    job.message = "正在按表和主键计算增删改并检测同行冲突…"
    result = three_way_merge(
        canonical_paths["base"],
        canonical_paths["revision"],
        canonical_paths["current"],
    )

    payload: dict[str, object] = {
        "kind": "merge",
        "merge_ok": result.ok,
        "tables": result.tables,
        "changes": _public_changes(result),
        "conflicts": _public_conflicts(result),
        "violations": [v.to_dict() for v in result.violations],
        "row_counts_base": result.row_counts_base,
        "row_counts_revision": result.row_counts_revision,
        "row_counts_current": result.row_counts_current,
        "row_counts_merged": result.row_counts_merged,
        "fk_edges": [
            {"table": s, "column": c, "parent_table": d, "parent_column": pc}
            for (s, c, d, pc) in result.fk_edges
        ],
        "download_available": False,
    }

    if not result.ok:
        job.status = "done"
        job.message = result.failure_reason or "合并失败，未生成合并库。"
        job.result = payload
        return

    job.message = "无冲突且外键有效，正在生成合并库…"
    merged_path = job_dir / "merged.db"
    write_merged_database(canonical_paths["current"], result, str(merged_path))

    payload["download_available"] = True
    payload["merged_path"] = "merged.db"
    job.status = "done"
    job.message = (
        f"合并成功：应用 {len(result.changes)} 项修订变更，"
        "当前库的其它补录全部保留，合并库外键有效。"
    )
    job.result = payload
