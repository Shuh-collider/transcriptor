"""MCP-сервер Транскриптора для Claude Desktop, Claude Code, Cursor и других MCP-клиентов (stdio).

Клиент запускает его так:  <папка проекта>\\.venv\\Scripts\\python.exe <папка проекта>\\transcriptor_mcp.py
Файл лежит в корне проекта, чтобы пакет transcriptor находился при любой рабочей папке клиента.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from transcriptor.__main__ import main  # noqa: E402

sys.argv = [sys.argv[0], "mcp"]
sys.exit(main())
