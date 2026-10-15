# Транскриптор

Локальная расшифровка видео с YouTube. Речь распознаётся на вашей видеокарте, спикеры размечаются автоматически, а Gemini или Claude пишет пересказ и делит видео на главы. Принимает ссылки на отдельные видео, плейлисты и целые каналы.

## Запуск

Дважды щёлкните `start.bat`: браузер откроется на http://127.0.0.1:8000.

Или из терминала в папке проекта:

```bash
.venv\Scripts\python.exe -m transcriptor
```

## Ключи (файл `.env`)

Без ключей расшифровка работает, но без спикеров и пересказа. Сервер перечитывает `.env` сам, перезапускать ничего не нужно.

| Строка | Зачем | Где взять |
|---|---|---|
| `HF_TOKEN=hf_…` | Кто говорит (pyannote) | Примите условия на [странице модели](https://huggingface.co/pyannote/speaker-diarization-community-1), затем создайте токен Read в [настройках Hugging Face](https://huggingface.co/settings/tokens) |
| `GEMINI_API_KEY=…` | Пересказ и главы через Gemini (есть бесплатный тариф) | [Google AI Studio](https://aistudio.google.com/apikey) |
| `ANTHROPIC_API_KEY=sk-ant-…` | Пересказ и главы через Claude | [Консоль Anthropic](https://console.anthropic.com/settings/keys), нужен положительный баланс |

Для пересказа достаточно одного из двух ключей. Модель выбирается в интерфейсе; если для выбранной нет ключа, берётся доступная.

## Как это устроено

```
ссылка → yt-dlp + Deno (только звук) → ffmpeg (16 кГц) → Silero VAD (фрагменты речи)
      → GigaAM v3 (русский) или Qwen3-ASR (английский и др.) → pyannote (кто говорит)
      → Gemini или Claude (пересказ, главное, главы, имена спикеров) → TXT / Markdown / JSON
```

| Этап | Технология | Почему она |
|---|---|---|
| Загрузка | [yt-dlp](https://github.com/yt-dlp/yt-dlp) + Deno | С 2025 года YouTube без JS-движка нормально не скачивается |
| Поиск речи | [Silero VAD](https://github.com/snakers4/silero-vad) | Режет запись по паузам, чтобы не разрывать слова |
| Русский | [GigaAM v3 e2e](https://github.com/salute-developers/GigaAM) (Сбер) | Лучшая открытая модель для русского: пунктуация, «ё», таймкоды слов |
| Английский и ещё 28 языков | [Qwen3-ASR 1.7B](https://github.com/QwenLM/Qwen3-ASR) + Qwen3-ForcedAligner | Одна из самых точных открытых моделей, сама определяет язык |
| Кто говорит | [pyannote community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) | Лучшая открытая диаризация, число спикеров не ограничено |
| Пересказ | [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models) или [Claude Opus 5](https://docs.anthropic.com) | Обе модели держат многочасовые расшифровки целиком; у Gemini есть бесплатный тариф |

Замеры на RTX 3080 Ti: интервью на 26 минут GigaAM распознаёт за 6 с, а весь путь от ссылки до текста без спикеров и пересказа укладывается в полминуты (не считая первой загрузки моделей). Qwen3-ASR с выравниванием слов работает примерно в 35 раз быстрее реального времени. Модели занимают 8–9 ГБ видеопамяти и выгружаются после 10 минут простоя.

## Подключение LLM (MCP)

Транскриптор работает как MCP-сервер. Claude, Cursor и другие MCP-клиенты сами ставят видео в очередь, ждут результат, читают расшифровки и ищут по ним.

| Инструмент | Что делает |
|---|---|
| `transcriptor_transcribe` | Ставит в очередь видео, плейлисты и каналы; уже расшифрованные пропускает |
| `transcriptor_get_jobs` | Статус заданий; умеет ждать завершения и сообщает прогресс |
| `transcriptor_get_summary` | Пересказ, главное, главы со ссылками на момент видео, спикеры |
| `transcriptor_get_transcript` | Текст по страницам, с фильтром по времени и спикеру |
| `transcriptor_search` | Поиск по всем расшифровкам со ссылкой на нужную секунду |
| `transcriptor_list_videos` | Библиотека готовых расшифровок |
| `transcriptor_summarize` | Сделать или переделать пересказ |
| `transcriptor_rename_speakers` | Подписать спикеров именами |
| `transcriptor_expand_url` | Посмотреть, что внутри плейлиста или канала |
| `transcriptor_list_jobs`, `transcriptor_cancel_job`, `transcriptor_retry_job` | Очередь |
| `transcriptor_get_status` | Видеокарта, ключи, доступные модели пересказа |

В примерах ниже `C:\\transcriptor` — замените на путь к папке проекта.

**Claude Desktop:** «Настройки → Разработчик → Изменить конфигурацию», добавьте в `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "transcriptor": {
      "command": "C:\\transcriptor\\.venv\\Scripts\\python.exe",
      "args": ["C:\\transcriptor\\transcriptor_mcp.py"]
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add transcriptor -- "C:\transcriptor\.venv\Scripts\python.exe" "C:\transcriptor\transcriptor_mcp.py"
```

**Клиенты с поддержкой HTTP** (Cursor и другие): пока запущен `start.bat`, MCP доступен по адресу `http://127.0.0.1:8000/mcp`.

Если веб-сервер не запущен, MCP поднимает его сам — фоном, без окна, лог в `data/server.log`. Такой сервер закрывается вместе с MCP-клиентом; чтобы он работал постоянно, запускайте `start.bat`.

## Консольный режим

```bash
.venv\Scripts\python.exe -m transcriptor run "https://youtu.be/VIDEO_ID"
```

Полезные параметры: `--lang ru|en`, `--speakers 2` (точное число спикеров), `--no-speakers`, `--no-summary`, `--model claude-sonnet-5`, `--summary-lang same`, `--engine qwen`. Ссылки на плейлисты и каналы обрабатываются целиком.

## Где результаты

`data/videos/<id видео>/`: звук, `transcript.json` (все слова с таймкодами), `video.json` (метаданные). Кнопки в интерфейсе выгружают TXT, Markdown и JSON.

## Если что-то не работает

- **«YouTube просит подтвердить, что вы не бот»**: впишите в `.env` строку `YTDLP_COOKIES_FROM_BROWSER=firefox` (или `chrome`, `edge`), указав браузер, где вы вошли в YouTube.
- **Ошибка 403 при скачивании** обычно проходит сама: программа повторяет загрузку через IPv4. Если нет, обновите yt-dlp командой `uv lock --upgrade-package yt-dlp` и затем `uv sync`.
- **Ошибки сертификатов**: HTTPS проверяется через хранилище сертификатов Windows (пакет `truststore`), поэтому антивирус или прокси, подменяющие сертификаты, не мешают.

## Установка с нуля

Нужны Windows, Python 3.11 и видеокарта NVIDIA (на процессоре тоже работает, но медленно).

```bash
git clone https://github.com/Shuh-collider/transcriptor
cd transcriptor
python -m pip install --user uv
uv sync
copy .env.example .env
```

`uv sync` поставит PyTorch с CUDA 12.8 и все зависимости (около 5 ГБ). Модели распознавания (~7 ГБ) скачаются при первой расшифровке. Ключи впишите в `.env` (см. «Ключи» выше).

## Лицензия

Код — [Apache 2.0](LICENSE). Модели (GigaAM, Qwen3-ASR, pyannote, Silero VAD) скачиваются с их страниц и распространяются под собственными лицензиями.
