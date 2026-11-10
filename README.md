# Transcriptor

[Русская версия](README.ru.md)

Transcribe YouTube videos on your own computer. Paste a link to a video, a playlist or a whole channel, and get:

- **the transcript** with timestamps, recognized on your NVIDIA GPU (GigaAM v3 for Russian, Qwen3-ASR for English and 28 more languages);
- **who is speaking** — speakers are labeled automatically and can be named;
- **on-screen text** (optional) — slides, captions, prompts and code shown in the video, read by Gemini;
- **a summary with chapters** written by Gemini or Claude, with clickable timestamps;
- exports to TXT, Markdown and JSON, and an **MCP server** so Claude and other AI assistants can queue videos, read and search all your transcripts.

The interface is in English and Russian (switch in the top bar). Everything except the optional summaries and on-screen text runs locally.

## Install (Windows)

Open **PowerShell** and run:

```powershell
irm https://raw.githubusercontent.com/Shuh-collider/transcriptor/main/install.ps1 | iex
```

The installer:

1. downloads Transcriptor to `%USERPROFILE%\transcriptor` (with git if you have it, otherwise as a ZIP);
2. installs [uv](https://docs.astral.sh/uv/), which brings its own Python 3.11;
3. **detects your graphics card** and installs the matching PyTorch build:

   | Your GPU | PyTorch build |
   |---|---|
   | NVIDIA GTX 16xx, RTX 20xx / 30xx / 40xx / 50xx and newer | CUDA 12.8 |
   | Older NVIDIA: GTX 9xx / 10xx | CUDA 12.6 |
   | No NVIDIA GPU (AMD, Intel, none) | CPU — works, but many times slower |

4. creates `.env` for your keys and a **Transcriptor** shortcut on the desktop.

The first install downloads about 5 GB of packages; the speech models (about 7 GB) are downloaded on the first transcription. **To update, run the same command again.**

Options (set before running the command): `$env:TRANSCRIPTOR_DIR = "D:\transcriptor"` to choose the folder, `$env:TRANSCRIPTOR_TORCH = "cpu"` (or `cu126`, `cu128`) to force a build, `$env:TRANSCRIPTOR_NO_SHORTCUT = "1"` to skip the shortcut.

Already downloaded the code? Run `install.bat` in its folder.

**Requirements:** Windows 10/11 64-bit, an NVIDIA GPU with 8 GB of memory or more is recommended (it also runs on the CPU), about 15 GB of free disk space.

## Keys (optional, free)

Transcription works without keys. Add them to `.env` in the install folder to unlock more; the server picks them up without a restart.

| Line in `.env` | What for | Where to get it |
|---|---|---|
| `HF_TOKEN=hf_…` | Who is speaking (pyannote) | Accept the terms on the [model page](https://huggingface.co/pyannote/speaker-diarization-community-1), then create a **Read** token in [Hugging Face settings](https://huggingface.co/settings/tokens) |
| `GEMINI_API_KEY=…` | Summaries, chapters and on-screen text (free tier available) | [Google AI Studio](https://aistudio.google.com/apikey) |
| `ANTHROPIC_API_KEY=sk-ant-…` | Summaries with Claude | [Anthropic Console](https://console.anthropic.com/settings/keys) |

One summary key is enough. On-screen text needs the Gemini key.

## Use

Double-click the **Transcriptor** shortcut (or `start.bat`). The browser opens http://127.0.0.1:8000.

1. Paste one or more links: videos, Shorts, playlists or channels. For playlists and channels you pick the videos.
2. Choose the options: video language (auto by default), speakers, **Read on-screen text**, summary model and language.
3. Click **Transcribe**. Videos are processed one at a time; a 30-minute video takes 1–2 minutes on an RTX 3080 Ti.
4. Read the summary and chapters, jump to any moment in the built-in player, search the text, rename speakers, export.

### On-screen text

Turn on **Read on-screen text** for videos where the important part is on the screen: demos, slides, screen recordings, music videos with captions. Transcriptor downloads the picture (up to 720p, without audio), takes a frame at every scene change and once a second, merges similar frames into "screens" and keeps the **last** frame of each screen, so text that is being typed or revealed line by line is complete. Gemini then writes out only new text, and it appears in the transcript as "On screen" lines and in the summary. It runs in parallel with speech recognition; a 25-minute video needs a few minutes and uses your Gemini quota.

## Connect AI assistants (MCP)

Transcriptor is an MCP server: Claude, Cursor and other MCP clients can queue videos, wait for results, read transcripts and search them.

| Tool | What it does |
|---|---|
| `transcriptor_transcribe` | Queue videos, playlists, channels (skips ones already transcribed; `screen_text` reads on-screen text) |
| `transcriptor_get_jobs` | Job status; can wait for completion and reports progress |
| `transcriptor_get_summary` | Summary, key points, chapters with links to the moment, speakers |
| `transcriptor_get_transcript` | Paged text with time and speaker filters, including on-screen text |
| `transcriptor_search` | Search all transcripts, with a link to the exact second |
| `transcriptor_list_videos` | The library of finished transcripts |
| `transcriptor_summarize` | Make or redo a summary |
| `transcriptor_rename_speakers` | Name the speakers |
| `transcriptor_expand_url` | See what is inside a playlist or channel |
| `transcriptor_list_jobs`, `transcriptor_cancel_job`, `transcriptor_retry_job` | The queue |
| `transcriptor_get_status` | GPU, keys, available summary models |

In the examples below replace `C:\Users\you\transcriptor` with your install folder.

**Claude Desktop:** Settings → Developer → Edit Config, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "transcriptor": {
      "command": "C:\\Users\\you\\transcriptor\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\you\\transcriptor\\transcriptor_mcp.py"]
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add transcriptor -- "C:\Users\you\transcriptor\.venv\Scripts\python.exe" "C:\Users\you\transcriptor\transcriptor_mcp.py"
```

**HTTP clients** (Cursor and others): while the app is running, MCP is available at `http://127.0.0.1:8000/mcp`.

If the web server is not running, the MCP server starts it in the background (log in `data/server.log`); that server stops together with the MCP client. To keep it running, start the app with the shortcut.

## Command line

```bash
.venv\Scripts\python.exe -m transcriptor run "https://youtu.be/VIDEO_ID"
```

Options: `--lang ru|en`, `--speakers 2` (exact number of speakers), `--no-speakers`, `--screen` (read on-screen text), `--no-summary`, `--model claude-sonnet-5`, `--summary-lang same|en|ru`, `--engine qwen`. Playlist and channel links are processed in full.

## How it works

```
link → yt-dlp + Deno (audio only) → ffmpeg (16 kHz) → Silero VAD (speech segments)
     → GigaAM v3 (Russian) or Qwen3-ASR (English and others) → pyannote (who is speaking)
     → [optional] video frames → Gemini (on-screen text)
     → Gemini or Claude (summary, key points, chapters, speaker names) → TXT / Markdown / JSON
```

| Stage | Technology | Why |
|---|---|---|
| Download | [yt-dlp](https://github.com/yt-dlp/yt-dlp) + Deno | Since 2025 YouTube cannot be downloaded properly without a JS engine |
| Speech detection | [Silero VAD](https://github.com/snakers4/silero-vad) | Cuts the recording at pauses so words are not split |
| Russian | [GigaAM v3 e2e](https://github.com/salute-developers/GigaAM) (Sber) | The best open model for Russian: punctuation, «ё», word timestamps |
| English and 28 more languages | [Qwen3-ASR 1.7B](https://github.com/QwenLM/Qwen3-ASR) + Qwen3-ForcedAligner | One of the most accurate open models; detects the language itself |
| Who is speaking | [pyannote community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) | The best open diarization, any number of speakers; refined per sentence with voice embeddings |
| On-screen text (optional) | ffmpeg + Gemini 3.8 Flash | A frame per "screen", the last one of each so typed text is complete; Gemini writes out only new text |
| Summary | [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models) or [Claude](https://docs.anthropic.com) | Both handle hours-long transcripts in one go; Gemini has a free tier |

Measured on an RTX 3080 Ti: GigaAM recognizes a 26-minute interview in 6 s, and the whole path from link to text without speakers and summary takes about half a minute (not counting the first model download). Qwen3-ASR with word alignment runs about 35× faster than real time. The models take 8–9 GB of GPU memory and are unloaded after 10 idle minutes.

## Where the results are

`data/videos/<video id>/`: the audio, `transcript.json` (all words with timestamps, on-screen text, summary), `video.json` (metadata), `frames/` (frames used for on-screen text). The buttons in the interface export TXT, Markdown and JSON.

## Troubleshooting

- **"YouTube asks to confirm you are not a bot"**: add `YTDLP_COOKIES_FROM_BROWSER=firefox` (or `chrome`, `edge`) to `.env` — the browser where you are signed in to YouTube. It may also pass by itself after a while.
- **Error 403 or very slow downloads**: Transcriptor retries over IPv4 and switches to IPv4 automatically when IPv6 to YouTube does not work. If it persists, update yt-dlp: `uv lock --upgrade-package yt-dlp`, then run the installer again.
- **The GPU is not used** (the status bar says "No GPU"): update the NVIDIA driver and run the installer again.
- **Certificate errors**: HTTPS is checked against the Windows certificate store (`truststore`), so antivirus software or proxies that replace certificates do not break it.

## License

Code: [Apache 2.0](LICENSE). The models (GigaAM, Qwen3-ASR, pyannote, Silero VAD) are downloaded from their pages and come with their own licenses.
