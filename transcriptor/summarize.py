"""Пересказ, главное, главы и имена спикеров через Gemini или Claude."""

from __future__ import annotations

import logging
import re
import time
from typing import Callable

from pydantic import BaseModel, ValidationError

from . import config
from .export import fmt_ts, parse_ts, timeline
from .i18n import L

log = logging.getLogger(__name__)

LANGUAGE_NAMES = {"ru": "Russian", "en": "English"}
Say = Callable[[str], None]


class SummaryError(RuntimeError):
    pass


class Chapter(BaseModel):
    start: str
    title: str
    summary: str


class SpeakerInfo(BaseModel):
    label: str
    name: str
    role: str


class SummaryResult(BaseModel):
    tldr: str
    summary: str
    key_points: list[str]
    chapters: list[Chapter]
    speakers: list[SpeakerInfo]


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STR = {"type": "string"}
SCHEMA = _object(
    {
        "tldr": _STR,
        "summary": _STR,
        "key_points": {"type": "array", "items": _STR},
        "chapters": {"type": "array", "items": _object({"start": _STR, "title": _STR, "summary": _STR})},
        "speakers": {"type": "array", "items": _object({"label": _STR, "name": _STR, "role": _STR})},
    }
)

SYSTEM = """You turn transcripts of YouTube videos into a faithful summary with chapters.

The transcript comes from automatic speech recognition, so expect occasional misheard words, especially names and terms. Use the title, description and context to interpret them, but never add facts that are not in the video. Speaker labels (S1, S2, …) come from automatic diarization and can be wrong near turn boundaries. Lines marked [ON SCREEN] are not speech: they are text shown in the video (slides, captions, interfaces), read from frames, with " / " between lines; use them as part of the content, especially when the video has little speech.

Write every text field in {language}.

- tldr: one or two sentences with the main point of the video.
- summary: 2–6 short paragraphs of plain prose (Markdown emphasis allowed, no headings) covering the main ideas, arguments, conclusions and notable details in the order they appear.
- key_points: 4–10 self-contained takeaways, one sentence each.
- chapters: split the whole video into chapters the way a careful YouTube creator would. The first chapter starts at 0:00, each covers one coherent topic, and together they span the video. Aim for a chapter every 3–8 minutes on long videos and 2–5 chapters on videos shorter than 10 minutes. `start` is the timestamp copied exactly from the transcript line where the topic begins; `title` is at most 60 characters; `summary` is one or two sentences.
- speakers: one entry per speaker label that appears in the transcript (an empty list if there are no labels). `name` is the person's name only when the video, title or description makes it clear who that speaker is; otherwise an empty string. `role` is a short description such as host, guest or narrator."""


# ---------- общее ----------


def render_transcript(doc: dict) -> str:
    hours = (doc.get("video") or {}).get("duration", 0) >= 3600
    lines = []
    for seg in timeline(doc):
        if seg.get("screen"):
            lines.append(f"[{fmt_ts(seg['start'], hours)}] [ON SCREEN] " + " / ".join(seg["text"].splitlines()))
            continue
        who = f" {seg['speaker']}:" if seg.get("speaker") else ""
        lines.append(f"[{fmt_ts(seg['start'], hours)}]{who} {seg['text']}")
    return "\n".join(lines)


def _video_block(doc: dict) -> str:
    video = doc.get("video") or {}
    parts = [
        f"Title: {video.get('title') or ''}",
        f"Channel: {video.get('channel') or ''}",
        f"Duration: {fmt_ts(video.get('duration') or 0)}",
    ]
    if video.get("upload_date"):
        d = video["upload_date"]
        parts.append(f"Uploaded: {d[:4]}-{d[4:6]}-{d[6:8]}")
    if video.get("view_count") is not None:
        parts.append(f"Views: {video['view_count']}")
    if video.get("chapters"):
        parts.append("Author's chapters: " + "; ".join(f"{fmt_ts(c['start'] or 0)} {c['title']}" for c in video["chapters"]))
    if video.get("description"):
        parts.append("Description:\n" + video["description"][:2000])
    return "\n".join(parts)


def _snap_chapters(chapters: list[Chapter], doc: dict) -> list[dict]:
    """Переводит время глав в секунды и притягивает к началу ближайшей реплики."""
    starts = sorted({seg["start"] for seg in timeline(doc)})
    duration = (doc.get("video") or {}).get("duration") or (starts[-1] if starts else 0)
    result: list[dict] = []
    for ch in chapters:
        t = parse_ts(ch.start)
        if t is None or t > duration:
            continue
        if starts:
            t = min(starts, key=lambda s: abs(s - t))
        if result and t <= result[-1]["start"]:
            continue
        result.append({"start": round(t, 2), "title": ch.title.strip(), "summary": ch.summary.strip()})
    if result:
        result[0]["start"] = 0.0
    return result


def summarize(doc: dict, model: str, language: str, progress: Say | None = None) -> dict:
    """language: код языка ответа (ru / en). Если для выбранной модели нет ключа, берётся доступная."""
    if not doc.get("segments") and not doc.get("screen"):
        raise SummaryError(L("Nothing to summarize: the video has neither speech nor on-screen text.", "Нечего пересказывать: в видео не найдено ни речи, ни надписей."))
    config.reload_env()
    model = config.resolve_summary_model(model)
    provider = config.SUMMARY_MODELS[model]["provider"]
    if not config.providers()[provider]:
        raise SummaryError(L("Summary not made: the .env file has neither GEMINI_API_KEY nor ANTHROPIC_API_KEY.", "Пересказ не сделан: в файле .env нет ни GEMINI_API_KEY, ни ANTHROPIC_API_KEY."))

    say = progress or (lambda _msg: None)
    system = SYSTEM.format(language=LANGUAGE_NAMES.get(language, "Russian"))
    user = f"<video>\n{_video_block(doc)}\n</video>\n\n<transcript>\n{render_transcript(doc)}\n</transcript>"
    call = call_gemini if provider == "gemini" else _call_claude
    text, used_model, usage, cost = call(system, user, model, say)

    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text)  # на случай, если модель обернёт JSON в ```
    try:
        result = SummaryResult.model_validate_json(text)
    except ValidationError as exc:
        log.warning("unexpected summary JSON: %s", text[:500])
        raise SummaryError(L("The model answered in an unexpected format; please try again.", "Модель вернула ответ в неожиданном формате — попробуйте ещё раз.")) from exc

    return {
        "model": used_model,
        "language": language,
        "tldr": result.tldr.strip(),
        "summary": result.summary.strip(),
        "key_points": [p.strip() for p in result.key_points if p.strip()],
        "chapters": _snap_chapters(result.chapters, doc),
        "speakers": {s.label: {"name": s.name.strip(), "role": s.role.strip()} for s in result.speakers},
        "usage": usage,
        "cost_usd": cost,
    }


# ---------- Gemini ----------


def _gemini_schema(node):
    """Gemini понимает не все ключевые слова JSON Schema — убираем additionalProperties."""
    if isinstance(node, dict):
        return {k: _gemini_schema(v) for k, v in node.items() if k != "additionalProperties"}
    if isinstance(node, list):
        return [_gemini_schema(v) for v in node]
    return node


def _gemini_error(exc: Exception, model: str) -> tuple[bool, str]:
    """(можно ли повторить, понятное сообщение)."""
    from google.genai._gaos.lib.compat_errors import APIConnectionError

    if isinstance(exc, APIConnectionError):
        return True, L("Cannot reach the Gemini API; check your internet connection.", "Нет связи с Gemini API — проверьте интернет.")
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    text = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
    text = str(text or getattr(exc, "message", None) or exc)
    low = text.lower()
    if "location is not supported" in low:
        return False, L("The Gemini API is not available in your country (a Google restriction).", "Gemini API недоступен в вашей стране — это ограничение Google.")
    if "api key not valid" in low or "api_key_invalid" in low or code == 401:
        return False, L("Google rejected GEMINI_API_KEY; check the key in .env.", "Google не принял GEMINI_API_KEY — проверьте ключ в .env.")
    if code == 403:
        return False, L(f"The key has no access to {model}: ", f"У ключа нет доступа к {model}: ") + text[:200]
    if code == 404:
        return False, L(f"Model {model} is not available for this key.", f"Модель {model} недоступна для этого ключа.")
    if code == 429:
        return True, L("Gemini rate limit exceeded (the free tier allows a few requests per minute). Try again later.", "Превышен лимит запросов Gemini (на бесплатном тарифе — несколько запросов в минуту). Попробуйте позже.")
    if code in (500, 502, 503, 504):
        return True, L("Gemini servers are overloaded; try again later.", "Серверы Gemini перегружены — попробуйте позже.")
    return False, L("Gemini API error", "Ошибка Gemini API") + f" ({code}): {text[:300]}"


def call_gemini(
    system: str,
    user: str | list[dict],
    model: str,
    say: Say,
    schema: dict = SCHEMA,
    thinking: str = "medium",
    reading: str | None = None,
    writing: str | None = None,
) -> tuple[str, str, dict, float | None]:
    """Запрос к Gemini с ответом в JSON по схеме. user — текст или список блоков (текст и картинки)."""
    import httpx
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai._gaos.errors import GenAiError
    from google.genai._gaos.lib.compat_errors import APIError as InteractionsError

    client = genai.Client()  # ключ берётся из GEMINI_API_KEY
    delays = [0, 10, 30]
    for attempt, delay in enumerate(delays):
        if delay:
            say(L(f"Gemini is busy, retrying in {delay} s…", f"Gemini занят, повторяю через {delay} с…"))
            time.sleep(delay)
        say(reading or L("Gemini is reading the transcript…", "Gemini читает расшифровку…"))
        parts: list[str] = []
        usage = None
        status = None
        try:
            stream = client.interactions.create(
                model=model,
                input=user,
                system_instruction=system,
                # новый формат: тип и схема внутри response_format; отдельный response_mime_type
                # относится к старому формату, и вместе с ним сервер отвечает 400
                response_format={"type": "text", "mime_type": "application/json", "schema": _gemini_schema(schema)},
                generation_config={"thinking_level": thinking, "max_output_tokens": 32000},
                store=False,  # не хранить запрос и ответ на стороне Google
                stream=True,
                timeout=600,
            )
            written = 0
            for event in stream:
                kind = getattr(event, "event_type", "")
                if kind == "step.delta" and getattr(event.delta, "type", "") == "text":
                    parts.append(event.delta.text)
                    written += len(event.delta.text)
                    say(f"{writing or L('Gemini is writing the summary…', 'Gemini пишет пересказ…')} {written} " + L("chars", "симв."))
                elif kind == "interaction.completed":
                    usage = getattr(event.interaction, "usage", None)
                    status = getattr(event.interaction, "status", None)
                elif kind == "error":
                    err = getattr(event, "error", None)
                    raise SummaryError(L("Gemini returned an error: ", "Gemini вернул ошибку: ") + str(getattr(err, "message", None) or err))
            break
        except (InteractionsError, genai_errors.APIError, GenAiError) as exc:
            retry, message = _gemini_error(exc, model)
            log.warning("gemini error: %s", exc)
            if not retry or attempt == len(delays) - 1:
                raise SummaryError(message) from exc
        except httpx.HTTPError as exc:
            if attempt == len(delays) - 1:
                raise SummaryError(L("Cannot reach the Gemini API; check your internet connection.", "Нет связи с Gemini API — проверьте интернет.")) from exc

    if status in ("incomplete", "budget_exceeded"):
        raise SummaryError(L("The answer did not fit into the output limit.", "Ответ не поместился в лимит модели."))
    if status in ("failed", "cancelled"):
        raise SummaryError(L("Gemini could not process the request.", "Gemini не смог обработать запрос."))
    text = "".join(parts)
    if not text.strip():
        raise SummaryError(L("Gemini returned an empty answer (perhaps a safety filter).", "Gemini вернул пустой ответ (возможно, сработал фильтр безопасности)."))
    tokens = {
        "input_tokens": getattr(usage, "total_input_tokens", None),
        "output_tokens": (getattr(usage, "total_output_tokens", None) or 0) + (getattr(usage, "total_thought_tokens", None) or 0),
    }
    return text, model, tokens, None


# ---------- Claude ----------


def _cost(model: str, usage) -> float | None:
    price = config.SUMMARY_MODELS.get(model)
    if not price or "input" not in price:
        return None
    inp = (usage.input_tokens or 0) + 1.25 * (usage.cache_creation_input_tokens or 0) + 0.1 * (
        usage.cache_read_input_tokens or 0
    )
    return round((inp * price["input"] + (usage.output_tokens or 0) * price["output"]) / 1_000_000, 4)


def _call_claude(system: str, user: str, model: str, say: Say) -> tuple[str, str, dict, float | None]:
    import anthropic

    client = anthropic.Anthropic(max_retries=3)
    params = {
        "model": model,
        "max_tokens": 32000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }
    if model != "claude-haiku-4-5":
        params["thinking"] = {"type": "adaptive"}
    try:
        if model == "claude-opus-5":
            # если классификаторы безопасности ошибочно откажут, сервер сам повторит запрос на другой модели
            stream_ctx = client.beta.messages.stream(**params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        else:
            stream_ctx = client.messages.stream(**params)
        say(L("Claude is reading the transcript…", "Claude читает расшифровку…"))
        written = 0
        with stream_ctx as stream:
            for event in stream:
                if event.type == "content_block_start" and event.content_block.type == "thinking":
                    say(L("Claude is analyzing the content…", "Claude анализирует содержание…"))
                elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                    written += len(event.delta.text)
                    say(L(f"Claude is writing the summary… {written} chars", f"Claude пишет пересказ… {written} симв."))
            final = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise SummaryError(L("Claude rejected ANTHROPIC_API_KEY; check it in .env.", "Claude не принял ключ ANTHROPIC_API_KEY — проверьте его в .env.")) from exc
    except anthropic.PermissionDeniedError as exc:
        raise SummaryError(L(f"The key has no access to {model}: ", f"У ключа нет доступа к {model}: ") + exc.message) from exc
    except anthropic.NotFoundError as exc:
        raise SummaryError(L(f"Model {model} is not available for this key.", f"Модель {model} недоступна для этого ключа.")) from exc
    except anthropic.RateLimitError as exc:
        raise SummaryError(L("Claude rate limit exceeded; try again later.", "Превышен лимит запросов к Claude — попробуйте позже.")) from exc
    except anthropic.BadRequestError as exc:
        raise SummaryError(L("Claude rejected the request: ", "Claude отклонил запрос: ") + exc.message) from exc
    except anthropic.APIStatusError as exc:
        raise SummaryError(L("Claude API error", "Ошибка Claude API") + f" ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise SummaryError(L("Cannot reach the Claude API; check your internet connection.", "Нет связи с Claude API — проверьте интернет.")) from exc

    if final.stop_reason == "refusal":
        raise SummaryError(L("Claude declined to summarize this video.", "Claude отказался пересказывать это видео."))
    if final.stop_reason == "max_tokens":
        raise SummaryError(L("The answer did not fit into the output limit.", "Ответ не поместился в лимит модели."))
    text = next((b.text for b in final.content if b.type == "text"), "")
    usage = final.usage
    tokens = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
    return text, final.model, tokens, _cost(final.model, usage)
