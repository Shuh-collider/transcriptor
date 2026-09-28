"""Запуск:

    python -m transcriptor              веб-интерфейс (http://127.0.0.1:8000, MCP — /mcp)
    python -m transcriptor run URL...   расшифровка в консоли
    python -m transcriptor mcp          MCP-сервер для LLM через stdio
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import warnings
import webbrowser

from . import config


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for name in ("httpx", "httpx2", "huggingface_hub", "urllib3", "pyannote", "lightning", "speechbrain"):
        logging.getLogger(name).setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)


def _port_state(host: str, port: int) -> str:
    """free — порт свободен, ours — там уже работает транскриптор, busy — занят чем-то другим."""
    import json
    import socket
    import urllib.request

    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex((host, port)) != 0:
            return "free"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=3) as resp:
            return "ours" if "version" in json.load(resp) else "busy"
    except Exception:  # noqa: BLE001 — любой ответ, кроме нашего API, значит «чужой»
        return "busy"


def serve(host: str, port: int, open_browser: bool) -> None:
    import uvicorn

    url = f"http://{host}:{port}"
    state = _port_state(host, port)
    if state == "ours":
        print(f"\n  Транскриптор уже запущен: {url}\n", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    if state == "busy":
        print(f"\n  Порт {port} занят другой программой. Запустите на другом: start.bat --port 8001\n", flush=True)
        return
    # MCP внутри веб-сервера ходит в его же API и не должен пытаться запустить второй сервер
    os.environ["TRANSCRIPTOR_URL"] = url
    os.environ["TRANSCRIPTOR_MCP_AUTOSTART"] = "0"
    if open_browser:
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    print(f"\n  Транскриптор запущен: {url}\n  MCP для LLM: {url}/mcp\n  Остановить: Ctrl+C\n", flush=True)
    uvicorn.run("transcriptor.server:app", host=host, port=port, log_level="warning")


def run_cli(args: argparse.Namespace) -> int:
    from .export import to_markdown, to_txt
    from .pipeline import Models, Options, summarize_video, transcribe_video, video_dir
    from .summarize import SummaryError
    from .youtube import DownloadError, expand

    options = Options.from_dict(
        {
            "language": args.lang,
            "ru_engine": args.engine,
            "diarize": not args.no_speakers,
            "num_speakers": args.speakers,
            "summarize": not args.no_summary,
            "summary_model": args.model,
            "summary_language": args.summary_lang,
        }
    )
    models = Models()
    failures = 0

    def report(stage: str, frac: float, message: str) -> None:
        print(f"\r  {message:<48} {frac * 100:5.1f}%", end="", flush=True)

    for url in args.urls:
        try:
            videos = expand(url).entries
        except DownloadError as exc:
            print(f"✗ {url}: {exc}")
            failures += 1
            continue
        for v in videos:
            print(f"\n▶ {v.title}  ({v.url})")
            started = time.time()
            try:
                doc = transcribe_video(v.id, v.url, options, models, report)
            except DownloadError as exc:
                print(f"\n✗ {exc}")
                failures += 1
                continue
            print(f"\r  Расшифровано за {time.time() - started:.0f} с: {len(doc['segments'])} реплик, "
                  f"язык {doc['language']}, {doc['engine']}" + " " * 10)
            for note in doc["notes"]:
                print(f"  ! {note}")
            if options.summarize and doc["segments"]:
                try:
                    doc = summarize_video(v.id, options.summary_model, options.summary_language,
                                          lambda msg: print(f"\r  {msg:<60}", end="", flush=True))
                    cost = doc["summary"].get("cost_usd")
                    print(f"\r  Пересказ готов ({doc['summary']['model']}" + (f", ${cost:.3f}" if cost else "") + ")" + " " * 30)
                except SummaryError as exc:
                    print(f"\r  ! {exc}" + " " * 30)
            folder = video_dir(v.id)
            (folder / "transcript.md").write_text(to_markdown(doc), encoding="utf-8")
            (folder / "transcript.txt").write_text(to_txt(doc), encoding="utf-8")
            print(f"  Файлы: {folder}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="transcriptor", description="Расшифровка видео с YouTube")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="веб-интерфейс (по умолчанию)")
    p_serve.add_argument("--host", default=config.HOST)
    p_serve.add_argument("--port", type=int, default=config.PORT)
    p_serve.add_argument("--no-browser", action="store_true", help="не открывать браузер")

    p_run = sub.add_parser("run", help="расшифровать ссылки в консоли")
    p_run.add_argument("urls", nargs="+", help="ссылки на видео, плейлисты или каналы")
    p_run.add_argument("--lang", default="auto", choices=["auto", "ru", "en"])
    p_run.add_argument("--engine", default="gigaam", choices=["gigaam", "qwen"], help="движок для русского")
    p_run.add_argument("--speakers", type=int, help="точное число спикеров, если известно")
    p_run.add_argument("--no-speakers", action="store_true", help="не определять спикеров")
    p_run.add_argument("--no-summary", action="store_true", help="без пересказа")
    p_run.add_argument("--model", default=config.default_summary_model(), choices=list(config.SUMMARY_MODELS))
    p_run.add_argument("--summary-lang", default="ru", choices=["ru", "en", "same"])

    sub.add_parser("mcp", help="MCP-сервер для LLM через stdio (Claude Desktop, Claude Code, Cursor…)")

    args = parser.parse_args()
    config.prepare_runtime()
    _setup_logging()  # логи идут в stderr: stdout в режиме mcp занят протоколом
    if args.cmd == "run":
        return run_cli(args)
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        return mcp_main()
    host = getattr(args, "host", config.HOST)
    port = getattr(args, "port", config.PORT)
    serve(host, port, open_browser=not getattr(args, "no_browser", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
