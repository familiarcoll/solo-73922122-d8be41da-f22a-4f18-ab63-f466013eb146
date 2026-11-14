"""FastAPI 入口：上传、轮询任务、查看结果、下载复现库。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .errors import JobError, ScriptRejected, ValidationError
from .jobs import registry
from .merge_service import run_merge_pipeline
from .samples import ensure_samples
from .service import run_pipeline

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_samples(STATIC_DIR)
    registry.cleanup_expired()

    async def _janitor() -> None:
        while True:
            await asyncio.sleep(60)
            registry.cleanup_expired()

    janitor = asyncio.create_task(_janitor())
    try:
        yield
    finally:
        janitor.cancel()


app = FastAPI(title="SQLite 复现库缩减服务", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/samples")
def list_samples() -> JSONResponse:
    return JSONResponse(ensure_samples(STATIC_DIR))


def _validate_upload_bytes(raw: bytes, label: str) -> None:
    if len(raw) == 0:
        raise ValidationError(f"{label}文件为空。")
    if len(raw) > config.MAX_DB_BYTES:
        raise ValidationError(
            f"{label}大小 {len(raw)} 字节，超过上限 {config.MAX_DB_BYTES} 字节。"
        )
    if not raw.startswith(b"SQLite format 3\x00"):
        raise ValidationError(f"{label}不是合法的 SQLite 数据库（魔数校验失败）。")


@app.post("/api/jobs")
async def create_job(
    db: UploadFile = File(..., description="满足约束的 SQLite 数据库"),
    sql: UploadFile | None = File(None, description="确定性 SQL 脚本文件"),
    script_text: str = Form("", description="或直接粘贴 SQL 脚本"),
) -> JSONResponse:
    raw_db = await db.read()
    _validate_upload_bytes(raw_db, "数据库")

    if sql is not None:
        raw_script = await sql.read()
    else:
        raw_script = script_text.encode("utf-8")
    if not raw_script.strip():
        raise ValidationError("请上传或粘贴 SQL 脚本。")

    job = registry.create()
    upload_path = job.dir / "upload.db"
    upload_path.write_bytes(raw_db)
    # 原件落盘后只读保存，任何试跑都只接触副本
    Path(upload_path).chmod(0o444)

    registry.run_in_background(job, run_pipeline, str(upload_path), raw_script)
    return JSONResponse({"id": job.id, "status": job.status}, status_code=202)


@app.post("/api/merge-jobs")
async def create_merge_job(
    base_db: UploadFile = File(..., description="基准库（共同祖先）"),
    revision_db: UploadFile = File(..., description="修订库（开发者修正）"),
    current_db: UploadFile = File(..., description="当前库（含新补录）"),
) -> JSONResponse:
    uploads = (
        ("base", base_db, "基准库"),
        ("revision", revision_db, "修订库"),
        ("current", current_db, "当前库"),
    )
    raw_files: dict[str, bytes] = {}
    for key, upload, label in uploads:
        raw = await upload.read()
        _validate_upload_bytes(raw, label)
        raw_files[key] = raw

    job = registry.create()
    saved_paths: dict[str, str] = {}
    for key, raw in raw_files.items():
        path = job.dir / f"upload_{key}.db"
        path.write_bytes(raw)
        # 三个上传原件全部只读保存，合并只接触规范副本
        path.chmod(0o444)
        saved_paths[key] = str(path)

    registry.run_in_background(
        job,
        run_merge_pipeline,
        saved_paths["base"],
        saved_paths["revision"],
        saved_paths["current"],
    )
    return JSONResponse({"id": job.id, "status": job.status}, status_code=202)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = registry.get(job_id)
    return JSONResponse(job.public_dict())


@app.get("/api/jobs/{job_id}/download")
def download_reduced(job_id: str) -> FileResponse:
    job = registry.get(job_id)
    if job.status != "done" or not job.result.get("download_available"):
        raise JobError("缩减结果尚不可下载。")
    reduced = job.dir / "reduced.db"
    if not reduced.exists():
        raise JobError("缩减库文件已被清理。")
    return FileResponse(
        reduced,
        media_type="application/x-sqlite3",
        filename="repro.db",
    )


@app.get("/api/merge-jobs/{job_id}/download")
def download_merged(job_id: str) -> FileResponse:
    job = registry.get(job_id)
    if job.result.get("kind") != "merge":
        raise JobError("该任务不是三方合并任务。")
    if job.status != "done" or not job.result.get("download_available"):
        raise JobError("合并结果尚不可下载（存在冲突或外键失效）。")
    merged = job.dir / "merged.db"
    if not merged.exists():
        raise JobError("合并库文件已被清理。")
    return FileResponse(
        merged,
        media_type="application/x-sqlite3",
        filename="merged.db",
    )


@app.exception_handler(ValidationError)
@app.exception_handler(ScriptRejected)
@app.exception_handler(JobError)
def _handle_bad_request(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})
