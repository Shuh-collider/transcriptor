"""Веб-интерфейс и HTTP API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import torch
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, config, library
from .export import description_links, to_markdown, to_txt
from .jobs import JobManager
from .mcp_server import mcp
from .pipeline import load_doc, refresh_video_meta, save_doc, video_dir
from .youtube import DownloadError, expand

WEB_DIR = Path(__file__).parent / "web"
manager: JobManager

# MCP по HTTP: http://127.0.0.1:8000/mcp (защита от DNS rebinding включается сама для 127.0.0.1)
_mcp_http = mcp.streamable_http_app(streamable_http_path="/mcp", host=config.HOST)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    manager = JobManager()
    manager.start()
    # Starlette не запускает lifespan вложенных приложений — сессии MCP запускаем здесь
    async with mcp.session_manager.run():
        yield
    manager.stop()


class _RevalidatedStatic(StaticFiles):
    """Без Cache-Control браузер кэширует JS «на глаз» и после обновления программы показывает
    старый интерфейс. no-cache заставляет каждый раз сверять ETag (ответ 304, если файл не менялся)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _asset_version() -> str:
    """Отпечаток файлов интерфейса: после обновления программы у файлов новый адрес,
    и браузер не может подсунуть старую копию из кэша."""
    digest = hashlib.sha1()
    for path in sorted(WEB_DIR.rglob("*")):
        if path.is_file():
            digest.update(f"{path.name}:{path.stat().st_mtime_ns}".encode())
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()
app = FastAPI(title="Транскриптор", version=__version__, lifespan=lifespan)
app.mount(f"/static/{ASSET_VERSION}", _RevalidatedStatic(directory=WEB_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    # относительные import в модулях тоже получат версию в пути
    html = html.replace('"/static/', f'"/static/{ASSET_VERSION}/')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
def status() -> dict:
    config.reload_env()
    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    providers = config.providers()
    return {
        "version": __version__,
        "gpu": gpu,
        "hf_token": bool(config.hf_token()),
        "providers": providers,
        "summary_models": {
            model_id: {**info, "available": providers[info["provider"]]}
            for model_id, info in config.SUMMARY_MODELS.items()
        },
        "default_summary_model": config.default_summary_model(),
        "env_file": str(config.ENV_FILE),
        **manager.status(),
    }


class ExpandIn(BaseModel):
    url: str
    limit: int = 300


@app.post("/api/expand")
async def api_expand(body: ExpandIn) -> dict:
    try:
        result = await run_in_threadpool(expand, body.url, max(1, min(body.limit, 5000)))
    except DownloadError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"kind": result.kind, "title": result.title, "entries": [e.to_dict() for e in result.entries]}


class VideoIn(BaseModel):
    id: str
    url: str
    title: str | None = None
    channel: str | None = None
    duration: float | None = None
    thumbnail: str | None = None


class SubmitIn(BaseModel):
    videos: list[VideoIn]
    options: dict = {}
    batch_title: str | None = None


@app.post("/api/jobs")
def submit(body: SubmitIn) -> list[dict]:
    if not body.videos:
        raise HTTPException(400, "Список видео пуст.")
    for v in body.videos:
        video_dir(v.id)  # проверка id
    return manager.submit([v.model_dump() for v in body.videos], body.options, body.batch_title)


@app.get("/api/jobs")
def jobs() -> list[dict]:
    return manager.list()


def _job_or_404(job: dict | None) -> dict:
    if not job:
        raise HTTPException(404, "Задание не найдено.")
    return job


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    return _job_or_404(manager.get(job_id))


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str) -> dict:
    return _job_or_404(manager.cancel(job_id))


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str) -> dict:
    return _job_or_404(manager.retry(job_id))


class SummarizeIn(BaseModel):
    model: str | None = None
    language: str | None = None


@app.post("/api/jobs/{job_id}/summarize")
def resummarize(job_id: str, body: SummarizeIn) -> dict:
    return _job_or_404(manager.resummarize(job_id, body.model, body.language))


@app.delete("/api/jobs/{job_id}")
def delete(job_id: str) -> dict:
    if not manager.delete(job_id):
        raise HTTPException(404, "Задание не найдено.")
    return {"ok": True}


@app.get("/api/videos")
def videos(query: str = "", limit: int = 20, offset: int = 0) -> dict:
    return library.list_videos(query, max(1, min(limit, 200)), max(0, offset))


@app.get("/api/search")
def search(q: str, video_id: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    return library.search(q, video_id, max(1, min(limit, 200)), max(0, offset))


def _doc_or_404(video_id: str) -> dict:
    try:
        doc = load_doc(video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if doc is None:
        raise HTTPException(404, "Расшифровка не найдена.")
    return doc


@app.get("/api/videos/{video_id}")
def video(video_id: str) -> dict:
    doc = _doc_or_404(video_id)
    doc["video"]["links"] = description_links(doc["video"].get("description"))
    return doc


@app.post("/api/videos/{video_id}/refresh")
async def refresh_video(video_id: str) -> dict:
    """Обновить дату загрузки, просмотры и лайки с YouTube."""
    try:
        return await run_in_threadpool(refresh_video_meta, video_id)
    except (ValueError, DownloadError) as exc:
        raise HTTPException(400, str(exc)) from exc


class SpeakersIn(BaseModel):
    names: dict[str, str]


@app.patch("/api/videos/{video_id}/speakers")
def rename_speakers(video_id: str, body: SpeakersIn) -> dict:
    doc = _doc_or_404(video_id)
    for label, name in body.names.items():
        if label in doc["speakers"]:
            doc["speakers"][label]["name"] = name.strip()[:80]
    save_doc(doc)
    return doc["speakers"]


def _filename(doc: dict, ext: str) -> str:
    title = re.sub(r'[\\/:*?"<>|\r\n]+', " ", doc["video"].get("title") or doc["video"]["id"]).strip()
    return f"{title[:120]}.{ext}"


@app.get("/api/videos/{video_id}/export.{fmt}")
def export(video_id: str, fmt: str) -> Response:
    doc = _doc_or_404(video_id)
    if fmt == "txt":
        body, media = to_txt(doc), "text/plain; charset=utf-8"
    elif fmt == "md":
        body, media = to_markdown(doc), "text/markdown; charset=utf-8"
    elif fmt == "json":
        body, media = json.dumps(doc, ensure_ascii=False, indent=1), "application/json; charset=utf-8"
    else:
        raise HTTPException(404, "Формат не поддерживается.")
    name = _filename(doc, fmt)
    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f"attachment; filename=\"transcript.{fmt}\"; filename*=UTF-8''{quote(name)}"},
    )


@app.get("/api/events")
async def events() -> StreamingResponse:
    queue = manager.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# MCP подключается последним: монтирование в корень ловит только пути, которых нет выше (/mcp)
app.mount("/", _mcp_http)
