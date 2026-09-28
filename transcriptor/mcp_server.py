"""MCP-сервер Транскриптора: LLM (Claude, Cursor и другие клиенты) сам ставит видео в очередь,
ждёт результат, читает расшифровки и ищет по ним.

Подключение:
* stdio — клиент запускает `python transcriptor_mcp.py`; если веб-сервер не запущен,
  он поднимается в фоне;
* HTTP — работающий веб-сервер отдаёт MCP по адресу http://127.0.0.1:8000/mcp.

Инструменты ходят в HTTP API веб-сервера, поэтому очередь и видеокарта общие с интерфейсом.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from typing import Annotated, Any, Literal

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__, config
from .export import fmt_ts, parse_ts

log = logging.getLogger(__name__)

INSTRUCTIONS = """Transcriptor transcribes YouTube videos on the user's own GPU (GigaAM v3 for Russian, Qwen3-ASR for English), labels who is speaking (pyannote) and writes summaries with chapters (Gemini or Claude).

Typical workflow:
1. transcriptor_list_videos / transcriptor_search — check whether the video is already transcribed.
2. transcriptor_transcribe — queue YouTube links (videos, playlists, channels); returns job ids.
3. transcriptor_get_jobs with wait_seconds (up to 45 per call) — repeat until the jobs are "done" (about 1–2 minutes per 30 minutes of video, plus ~10–30 s for the summary).
4. transcriptor_get_summary (summary, key points, chapters, speakers) or transcriptor_get_transcript (full text, paginated).

Transcripts are usually in Russian. Timestamps are measured from the start of the video; links with &t= open YouTube at that moment."""

mcp = MCPServer("transcriptor_mcp", title="Транскриптор", instructions=INSTRUCTIONS, version=__version__)

FINAL = {"done", "error", "canceled"}
ACTIVE = {"queued", "running", "summarizing"}
# Клиенты MCP обрывают вызов инструмента примерно через минуту, поэтому одно ожидание короче;
# дольше ждём повторными вызовами
MAX_WAIT = 45
Format = Annotated[Literal["markdown", "json"], Field(description="'markdown' for reading, 'json' for structured data")]
VideoId = Annotated[str, Field(min_length=6, max_length=20, description="YouTube video id, e.g. '9mQN4E6ur-w'")]

_start_lock = asyncio.Lock()


# ---------- связь с веб-сервером ----------


def _base_url() -> str:
    return os.getenv("TRANSCRIPTOR_URL") or f"http://{config.HOST}:{config.PORT}"


async def _server_up(client: httpx.AsyncClient) -> bool:
    try:
        return (await client.get("/api/status", timeout=3)).status_code == 200
    except httpx.HTTPError:
        return False


def _spawn_server() -> None:
    """Запускает веб-сервер в фоне (без окна), вывод — в data/server.log."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "transcriptor", "serve", "--no-browser"]
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    with open(config.DATA_DIR / "server.log", "ab") as log_file:
        kwargs = dict(cwd=config.ROOT, stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file)
        try:
            # сервер должен пережить MCP-процесс, даже если клиент держит его в задании Windows
            subprocess.Popen(cmd, creationflags=flags | breakaway, **kwargs)
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kwargs)


async def _ensure_server() -> None:
    async with httpx.AsyncClient(base_url=_base_url()) as client:
        if await _server_up(client):
            return
        if os.getenv("TRANSCRIPTOR_MCP_AUTOSTART", "1") == "0":
            raise ToolError(f"Transcriptor is not running at {_base_url()}. Ask the user to start it (start.bat).")
        async with _start_lock:
            if await _server_up(client):
                return
            log.info("starting the Transcriptor web server")
            _spawn_server()
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                await asyncio.sleep(1.5)
                if await _server_up(client):
                    return
    raise ToolError(f"Could not start Transcriptor at {_base_url()}; see data/server.log in the project folder.")


async def _api(method: str, path: str, **kwargs: Any) -> Any:
    await _ensure_server()
    try:
        async with httpx.AsyncClient(base_url=_base_url(), timeout=httpx.Timeout(180, connect=5)) as client:
            resp = await client.request(method, path, **kwargs)
    except httpx.TimeoutException as exc:
        raise ToolError("Transcriptor did not answer in time; try again.") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Cannot reach Transcriptor at {_base_url()}: {exc}") from exc
    if resp.status_code == 404 and path.startswith("/api/videos/"):
        raise ToolError("No transcript for this video id. See transcriptor_list_videos, or queue it with transcriptor_transcribe.")
    if resp.status_code == 404 and path.startswith("/api/jobs/"):
        raise ToolError("Unknown job id. See transcriptor_list_jobs.")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text[:300]
        raise ToolError(f"{detail or resp.reason_phrase} (HTTP {resp.status_code})")
    return resp.json()


# ---------- оформление ----------


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def _speaker(doc: dict, label: str | None) -> str:
    if not label:
        return ""
    return ((doc.get("speakers") or {}).get(label) or {}).get("name") or f"Speaker {label.lstrip('S')}"


def _stats(video: dict) -> str:
    """'uploaded 2012-03-17, 82,169 views, 761 likes (as of 2026-09-28)'."""
    parts = []
    date = video.get("upload_date") or ""
    if len(date) == 8:
        parts.append(f"uploaded {date[:4]}-{date[4:6]}-{date[6:]}")
    if video.get("view_count") is not None:
        parts.append(f"{video['view_count']:,} views")
    if video.get("like_count") is not None:
        parts.append(f"{video['like_count']:,} likes")
    if video.get("view_count") is not None and video.get("stats_at"):
        parts[-1] += f" (as of {video['stats_at'][:10]})"
    return ", ".join(parts)


def _at(url: str | None, seconds: float) -> str:
    return f"{url}{'&' if '?' in (url or '') else '?'}t={int(seconds)}s" if url else ""


def _job_line(job: dict) -> str:
    extra = ""
    if job["status"] == "running":
        extra = f" {round((job.get('progress') or 0) * 100)}% — {job.get('message') or ''}"
    elif job["status"] == "summarizing":
        extra = f" — {job.get('message') or 'writing the summary'}"
    elif job.get("error"):
        extra = f" — {job['error']}"
    notes = "".join(f"\n  - note: {n}" for n in job.get("notes") or [])
    title = job.get("title") or job["url"]
    return (
        f"- job `{job['id']}` **{job['status']}**{extra}\n"
        f"  {title} (video `{job['video_id']}`, {fmt_ts(job.get('duration') or 0)}){notes}"
    )


def _parse_time(value: str | None) -> float | None:
    if value is None or not str(value).strip():
        return None
    parsed = parse_ts(value)
    if parsed is not None:
        return parsed
    try:
        return float(value)
    except ValueError as exc:
        raise ToolError(f"Cannot read time {value!r}; use '12:34', '1:02:03' or seconds.") from exc


# ---------- инструменты: состояние и очередь ----------


@mcp.tool(
    name="transcriptor_get_status",
    annotations=ToolAnnotations(title="Transcriptor status", readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
)
async def transcriptor_get_status() -> str:
    """Check that Transcriptor is running and what it can do: GPU, speaker labelling, available summary
    models (depends on Gemini/Claude API keys), models loaded in GPU memory and the job queue.
    Call it first when another tool fails or to learn valid summary_model ids."""
    s = await _api("GET", "/api/status")
    models = [f"`{mid}` ({m['title']})" for mid, m in s["summary_models"].items() if m.get("available")]
    counts = ", ".join(f"{k}: {v}" for k, v in (s.get("counts") or {}).items()) or "empty"
    return "\n".join(
        [
            f"Transcriptor {s['version']} is running at {_base_url()}",
            f"- GPU: {s['gpu'] or 'none (CPU only, slow)'}",
            f"- Speaker labels: {'available' if s['hf_token'] else 'unavailable (no HF_TOKEN in .env)'}",
            f"- Summary models: {', '.join(models) if models else 'none (no GEMINI_API_KEY or ANTHROPIC_API_KEY in .env)'}",
            f"- Default summary model: `{s['default_summary_model']}`",
            f"- Models in GPU memory: {', '.join(s.get('models_loaded') or []) or 'none'}",
            f"- Jobs: {counts}",
        ]
    )


@mcp.tool(
    name="transcriptor_expand_url",
    annotations=ToolAnnotations(title="Preview a YouTube link", readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
async def transcriptor_expand_url(
    url: Annotated[str, Field(description="YouTube link: video, Shorts, playlist or channel (https://www.youtube.com/@name)")],
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum videos to list for playlists and channels")] = 50,
) -> str:
    """List the videos behind a YouTube link without transcribing anything. Use it to see what a
    playlist or channel contains before choosing what to pass to transcriptor_transcribe.
    Channels are listed newest first."""
    data = await _api("POST", "/api/expand", json={"url": url, "limit": limit})
    lines = [f"{data['kind']}: {data['title']} — {len(data['entries'])} video(s)"]
    for v in data["entries"]:
        views = f", ~{v['view_count']:,} views" if v.get("view_count") else ""
        lines.append(f"- `{v['id']}` {v['title']} ({fmt_ts(v.get('duration') or 0)}{views}) {v['url']}")
    return "\n".join(lines)


@mcp.tool(
    name="transcriptor_transcribe",
    annotations=ToolAnnotations(title="Transcribe YouTube videos", readOnlyHint=False, destructiveHint=False,
                                idempotentHint=False, openWorldHint=True),
)
async def transcriptor_transcribe(
    urls: Annotated[list[str], Field(min_length=1, max_length=20, description="YouTube links: videos, Shorts, playlists or channels")],
    language: Annotated[Literal["auto", "ru", "en"], Field(description="Speech language; 'auto' detects it")] = "auto",
    speakers: Annotated[bool, Field(description="Label who is speaking")] = True,
    num_speakers: Annotated[int | None, Field(ge=1, le=20, description="Exact number of speakers, if known")] = None,
    summarize: Annotated[bool, Field(description="Also write a summary with chapters")] = True,
    summary_model: Annotated[str | None, Field(description="Summary model id from transcriptor_get_status; default if omitted")] = None,
    summary_language: Annotated[Literal["ru", "en", "same"], Field(description="Summary language; 'same' = language of the video")] = "ru",
    max_videos_per_link: Annotated[int, Field(ge=1, le=50, description="For playlists/channels: how many videos to queue")] = 5,
    skip_existing: Annotated[bool, Field(description="Skip videos that already have a transcript")] = True,
) -> str:
    """Queue YouTube videos for transcription on the local GPU, with optional speaker labels and summary.
    Returns job ids immediately — call transcriptor_get_jobs(job_ids, wait_seconds=...) to wait for results.
    Videos are processed one at a time; a 30-minute video takes about 1–2 minutes."""
    options = {
        "language": language,
        "diarize": speakers,
        "num_speakers": num_speakers,
        "summarize": summarize,
        "summary_model": summary_model,
        "summary_language": summary_language,
    }
    queued: list[dict] = []
    skipped: list[str] = []
    problems: list[str] = []
    for url in urls:
        try:
            data = await _api("POST", "/api/expand", json={"url": url, "limit": max_videos_per_link})
        except ToolError as exc:
            problems.append(f"{url}: {exc}")
            continue
        videos = []
        for video in data["entries"][:max_videos_per_link]:
            if skip_existing:
                try:
                    await _api("GET", f"/api/videos/{video['id']}")
                    skipped.append(f"`{video['id']}` {video['title']}")
                    continue
                except ToolError:
                    pass  # расшифровки ещё нет
            videos.append(video)
        if videos:
            batch = data["title"] if data["kind"] != "video" else None
            queued += await _api("POST", "/api/jobs", json={"videos": videos, "options": options, "batch_title": batch})
    lines = []
    if queued:
        lines.append(f"Queued {len(queued)} job(s):")
        lines += [f"- job `{j['id']}` — {j.get('title') or j['url']} (video `{j['video_id']}`)" for j in queued]
        lines.append(f"Wait with transcriptor_get_jobs(job_ids={[j['id'] for j in queued]}, wait_seconds={MAX_WAIT}); repeat until done.")
    if skipped:
        lines.append("Already transcribed (use transcriptor_get_summary / transcriptor_get_transcript, "
                     "or skip_existing=false to redo): " + "; ".join(skipped))
    if problems:
        lines.append("Could not queue: " + "; ".join(problems))
    return "\n".join(lines) or "Nothing to do."


@mcp.tool(
    name="transcriptor_get_jobs",
    annotations=ToolAnnotations(title="Job status (optionally wait)", readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
)
async def transcriptor_get_jobs(
    job_ids: Annotated[list[str], Field(min_length=1, max_length=50, description="Job ids from transcriptor_transcribe")],
    ctx: Context,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description=f"Wait up to this long (max {MAX_WAIT} s) until all jobs finish; 0 = just check")] = MAX_WAIT,
) -> str:
    """Get the status of transcription jobs: queued, running (with progress), summarizing, done, error or
    canceled. With wait_seconds > 0 it waits until every job is finished (done/error/canceled) or the time
    runs out, reporting progress; if jobs are still running, simply call it again. When a job is done, read
    it with transcriptor_get_summary or transcriptor_get_transcript using its video id."""
    deadline = time.monotonic() + wait_seconds
    while True:
        jobs = [await _api("GET", f"/api/jobs/{job_id}") for job_id in job_ids]
        pending = [j for j in jobs if j["status"] not in FINAL]
        if not pending or time.monotonic() >= deadline:
            break
        done_share = sum(1.0 if j["status"] in FINAL else 0.95 if j["status"] == "summarizing" else (j.get("progress") or 0) * 0.95 for j in jobs)
        current = next((j for j in pending if j["status"] != "queued"), pending[0])
        await ctx.report_progress(round(100 * done_share / len(jobs)), 100, f"{current.get('title') or current['video_id']}: {current.get('message') or current['status']}")
        await asyncio.sleep(3)
    lines = [_job_line(j) for j in jobs]
    if pending:
        lines.append(f"{len(pending)} job(s) still in progress — call again with wait_seconds to keep waiting.")
    done = [j["video_id"] for j in jobs if j["status"] == "done"]
    if done:
        lines.append(f"Read results with transcriptor_get_summary / transcriptor_get_transcript (video ids: {', '.join(done)}).")
    return "\n".join(lines)


@mcp.tool(
    name="transcriptor_list_jobs",
    annotations=ToolAnnotations(title="List jobs", readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
)
async def transcriptor_list_jobs(
    status: Annotated[Literal["all", "active", "done", "error"], Field(description="'active' = queued/running/summarizing; 'error' includes canceled")] = "all",
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum jobs to return")] = 20,
    offset: Annotated[int, Field(ge=0, description="Jobs to skip, for pagination")] = 0,
    response_format: Format = "markdown",
) -> str:
    """List transcription jobs, newest first, with status and progress."""
    jobs = await _api("GET", "/api/jobs")
    wanted = {"active": ACTIVE, "done": {"done"}, "error": {"error", "canceled"}}.get(status)
    if wanted:
        jobs = [j for j in jobs if j["status"] in wanted]
    page = jobs[offset : offset + limit]
    more = offset + len(page) < len(jobs)
    if response_format == "json":
        return _json({"total": len(jobs), "offset": offset, "has_more": more, "next_offset": offset + len(page) if more else None, "jobs": page})
    lines = [f"{len(jobs)} job(s), showing {len(page)} from {offset}:"] + [_job_line(j) for j in page]
    if more:
        lines.append(f"More: offset={offset + len(page)}")
    return "\n".join(lines)


@mcp.tool(
    name="transcriptor_cancel_job",
    annotations=ToolAnnotations(title="Cancel a job", readOnlyHint=False, destructiveHint=True,
                                idempotentHint=True, openWorldHint=False),
)
async def transcriptor_cancel_job(job_id: Annotated[str, Field(description="Job id to stop")]) -> str:
    """Stop a queued or running job. Finished transcripts are not deleted."""
    job = await _api("POST", f"/api/jobs/{job_id}/cancel")
    return _job_line(job)


@mcp.tool(
    name="transcriptor_retry_job",
    annotations=ToolAnnotations(title="Retry a job", readOnlyHint=False, destructiveHint=False,
                                idempotentHint=False, openWorldHint=True),
)
async def transcriptor_retry_job(job_id: Annotated[str, Field(description="Job id that ended with error or was canceled")]) -> str:
    """Put a failed or canceled job back into the queue with the same options."""
    job = await _api("POST", f"/api/jobs/{job_id}/retry")
    return _job_line(job)


# ---------- инструменты: готовые расшифровки ----------


@mcp.tool(
    name="transcriptor_list_videos",
    annotations=ToolAnnotations(title="List transcribed videos", readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
)
async def transcriptor_list_videos(
    query: Annotated[str, Field(description="Filter by words in the title, channel, speaker names or summary; empty = all")] = "",
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum videos to return")] = 20,
    offset: Annotated[int, Field(ge=0, description="Videos to skip, for pagination")] = 0,
    response_format: Format = "markdown",
) -> str:
    """List videos that already have a transcript, newest first: title, channel, duration, language,
    YouTube upload date and views, speakers and a one-line summary if there is one."""
    params = {"query": query, "limit": limit, "offset": offset}
    data = await _api("GET", "/api/videos", params=params)
    if response_format == "json":
        return _json(data)
    if not data["items"]:
        return "No transcribed videos match." if query else "No videos have been transcribed yet."
    lines = [f"{data['total']} video(s), showing {data['count']} from {offset}:"]
    for v in data["items"]:
        speakers = ", ".join(f"{k}={n}" for k, n in v["speakers"].items())
        stats = f", {_stats(v)}" if _stats(v) else ""
        lines.append(
            f"- `{v['video_id']}` **{v['title']}** — {v['channel']}, {fmt_ts(v.get('duration') or 0)}, "
            f"{v['language']}{stats}{', speakers: ' + speakers if speakers else ''}{', has summary' if v['has_summary'] else ''}"
        )
        if v.get("tldr"):
            lines.append(f"  {v['tldr']}")
    if data["has_more"]:
        lines.append(f"More: offset={data['next_offset']}")
    return "\n".join(lines)


@mcp.tool(
    name="transcriptor_get_summary",
    annotations=ToolAnnotations(title="Summary and chapters", readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
)
async def transcriptor_get_summary(video_id: VideoId, response_format: Format = "markdown") -> str:
    """Get the summary of a transcribed video: one-line gist, summary paragraphs, key points, chapters with
    timestamps and YouTube links, speakers with names, roles and share of speaking time, and the links from
    the video description. If there is no summary yet, create it with transcriptor_summarize."""
    doc = await _api("GET", f"/api/videos/{video_id}")
    video, summary = doc["video"], doc.get("summary")
    speakers = doc.get("speakers") or {}
    total = sum(s.get("seconds", 0) for s in speakers.values()) or 1
    if response_format == "json":
        return _json({"video": video, "language": doc.get("language"), "summary": summary,
                      "summary_error": doc.get("summary_error"), "speakers": speakers})
    meta = [video.get("channel"), fmt_ts(video.get("duration") or 0), _stats(video), video["url"]]
    lines = [f"# {video['title']}", " · ".join(m for m in meta if m), ""]
    if not summary:
        reason = doc.get("summary_error") or "it was not requested"
        lines.append(f"No summary yet ({reason}). Create one with transcriptor_summarize(video_id='{video_id}').")
    else:
        lines += [f"**In short:** {summary['tldr']}", "", "## Summary", summary["summary"], ""]
        if summary.get("key_points"):
            lines += ["## Key points"] + [f"- {p}" for p in summary["key_points"]] + [""]
        if summary.get("chapters"):
            lines.append("## Chapters")
            for c in summary["chapters"]:
                lines.append(f"- [{fmt_ts(c['start'])}]({_at(video['url'], c['start'])}) **{c['title']}** — {c['summary']}")
            lines.append("")
        lines.append(f"_Summary by {summary.get('model')}_")
    if speakers:
        lines += ["", "## Speakers"]
        for label, info in speakers.items():
            role = f", {info['role']}" if info.get("role") else ""
            lines.append(f"- {label}: {_speaker(doc, label)}{role} — {round(100 * info.get('seconds', 0) / total)}% of speech")
    if video.get("links"):
        lines += ["", "## Links from the video description"]
        lines += [f"- {link['label']}: {link['url']}" if link["label"] else f"- {link['url']}" for link in video["links"]]
    return "\n".join(lines)


@mcp.tool(
    name="transcriptor_get_transcript",
    annotations=ToolAnnotations(title="Read a transcript", readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
)
async def transcriptor_get_transcript(
    video_id: VideoId,
    start: Annotated[str | None, Field(description="Only from this moment: '12:34', '1:02:03' or seconds")] = None,
    end: Annotated[str | None, Field(description="Only up to this moment, same formats as start")] = None,
    speaker: Annotated[str | None, Field(description="Only this speaker: label like 'S1' or part of the name")] = None,
    offset: Annotated[int, Field(ge=0, description="Segments to skip, for pagination (use next_offset)")] = 0,
    max_chars: Annotated[int, Field(ge=1000, le=100_000, description="Maximum characters of text to return")] = 20_000,
    response_format: Format = "markdown",
) -> str:
    """Read the transcript of a video as timestamped lines '[m:ss] Speaker: text', optionally only a time range
    or one speaker. Long transcripts are returned in pages: follow next_offset until has_more is false.
    For an overview prefer transcriptor_get_summary; to find a topic use transcriptor_search."""
    doc = await _api("GET", f"/api/videos/{video_id}")
    video = doc["video"]
    t0, t1 = _parse_time(start), _parse_time(end)
    wanted = (speaker or "").strip().lower()
    segments = [
        s for s in doc["segments"]
        if (t0 is None or s["end"] >= t0) and (t1 is None or s["start"] <= t1)
        and (not wanted or wanted == (s.get("speaker") or "").lower() or wanted in _speaker(doc, s.get("speaker")).lower())
    ]
    page, used = [], 0
    for seg in segments[offset:]:
        if page and used + len(seg["text"]) > max_chars:
            break
        page.append(seg)
        used += len(seg["text"])
    next_offset = offset + len(page)
    more = next_offset < len(segments)
    hours = (video.get("duration") or 0) >= 3600
    if response_format == "json":
        return _json(
            {
                "video_id": video_id, "title": video["title"], "url": video["url"], "language": doc.get("language"),
                "total_segments": len(segments), "offset": offset, "has_more": more,
                "next_offset": next_offset if more else None,
                "segments": [
                    {"start": s["start"], "end": s["end"], "speaker": s.get("speaker"),
                     "speaker_name": _speaker(doc, s.get("speaker")), "text": s["text"]}
                    for s in page
                ],
            }
        )
    if not segments:
        return "No speech matches these filters."
    header = [
        f"# {video['title']}",
        f"{video['url']} · {doc.get('language')} · segments {offset + 1}–{next_offset} of {len(segments)}",
        "",
    ]
    body = []
    for s in page:
        who = f" {_speaker(doc, s.get('speaker'))}:" if s.get("speaker") else ""
        body.append(f"[{fmt_ts(s['start'], hours)}]{who} {s['text']}")
    footer = [f"", f"has_more: true — call again with offset={next_offset}"] if more else []
    return "\n".join(header + body + footer)


@mcp.tool(
    name="transcriptor_search",
    annotations=ToolAnnotations(title="Search transcripts", readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
)
async def transcriptor_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Words to find; all must occur in the same line (case- and ё/е-insensitive)")],
    video_id: Annotated[str | None, Field(description="Search only this video; omit to search all transcripts")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum matches to return")] = 20,
    offset: Annotated[int, Field(ge=0, description="Matches to skip, for pagination")] = 0,
    response_format: Format = "markdown",
) -> str:
    """Find where something is said across all transcribed videos (or one video). Returns matching lines with
    video, timestamp, speaker and a YouTube link that opens at that moment. Transcripts are mostly Russian —
    search in the language of the video."""
    params = {"q": query, "limit": limit, "offset": offset}
    if video_id:
        params["video_id"] = video_id
    data = await _api("GET", "/api/search", params=params)
    if response_format == "json":
        return _json(data)
    if not data["items"]:
        return f"Nothing found for {query!r}."
    lines = [f"{data['total']} match(es) for {query!r}, showing {data['count']} from {offset}:"]
    for hit in data["items"]:
        who = f" {hit['speaker_name']}:" if hit.get("speaker_name") else ""
        lines.append(f"- [{fmt_ts(hit['start'])}]({hit['url']}) **{hit['title']}** (`{hit['video_id']}`){who} {hit['snippet']}")
    if data["has_more"]:
        lines.append(f"More: offset={data['next_offset']}")
    return "\n".join(lines)


# ---------- инструменты: правка ----------


@mcp.tool(
    name="transcriptor_summarize",
    annotations=ToolAnnotations(title="Write or redo the summary", readOnlyHint=False, destructiveHint=False,
                                idempotentHint=False, openWorldHint=True),
)
async def transcriptor_summarize(
    video_id: VideoId,
    ctx: Context,
    model: Annotated[str | None, Field(description="Summary model id from transcriptor_get_status; default if omitted")] = None,
    language: Annotated[Literal["ru", "en", "same"], Field(description="Summary language; 'same' = language of the video")] = "ru",
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT, description=f"Wait for the result up to this long (max {MAX_WAIT} s); 0 = just start")] = MAX_WAIT,
) -> str:
    """Create or replace the summary, key points, chapters and speaker names of an already transcribed video
    using Gemini or Claude. Returns the new summary when it is ready within wait_seconds."""
    jobs = await _api("GET", "/api/jobs")
    finished = [j for j in jobs if j["video_id"] == video_id and j["status"] in FINAL]
    if any(j["video_id"] == video_id and j["status"] in ACTIVE for j in jobs):
        raise ToolError("This video is still being processed; wait with transcriptor_get_jobs first.")
    if not finished:
        raise ToolError(f"No finished transcription for video {video_id}; transcribe it with transcriptor_transcribe.")
    job = finished[0]
    await _api("POST", f"/api/jobs/{job['id']}/summarize", json={"model": model, "language": language})
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        current = await _api("GET", f"/api/jobs/{job['id']}")
        if current["status"] != "summarizing":
            break
        await ctx.report_progress(50, 100, current.get("message") or "writing the summary")
        await asyncio.sleep(2)
    else:
        return f"The summary is still being written (job `{job['id']}`); check later with transcriptor_get_summary."
    return await transcriptor_get_summary(video_id)


@mcp.tool(
    name="transcriptor_rename_speakers",
    annotations=ToolAnnotations(title="Rename speakers", readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
)
async def transcriptor_rename_speakers(
    video_id: VideoId,
    names: Annotated[dict[str, str], Field(description="Speaker label → name, e.g. {'S1': 'Anna', 'S2': 'Host'}; empty name resets")],
) -> str:
    """Give speakers of a video human names. The names appear in transcripts, summaries, search and exports."""
    speakers = await _api("PATCH", f"/api/videos/{video_id}/speakers", json={"names": names})
    return "Speakers now: " + ", ".join(f"{k} = {v.get('name') or '(unnamed)'}" for k, v in speakers.items())


def main() -> int:
    mcp.run("stdio")
    return 0
