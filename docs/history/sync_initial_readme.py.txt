"""Historical one-off helper (archived text, not an active project command).

Run with the project venv::

    MCP\\.venv\\Scripts\\python.exe MCP\\tests\\sync_readme.py
"""

from __future__ import annotations

from pathlib import Path

MCP_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = MCP_DIR.parent
ROOT_README = WORKSPACE / "README.md"
MCP_README = MCP_DIR / "README.md"

MARKER = "# Blender MCP（Codex 用）— 几何节点 / 形态键 / 驱动器\n"

NOTE = (
    "# Blender MCP（Codex 用）— 几何节点 / 形态键 / 驱动器\n"
    "\n"
    "> 本文件是 MCP 项目根目录的 README；与 `<workspace>/README.md` 内容一致（同一份文档的两个副本）。\n"
    "> 架构约定见 `docs/ARCHITECTURE.md`，实测证据见 `tests/reports/TEST_REPORT.md`，"
    "开发日志见 `docs/DEVELOPMENT_LOG.md`。\n"
)


def main() -> int:
    text = ROOT_README.read_text(encoding="utf-8")
    if not text.startswith(MARKER):
        raise SystemExit("workspace README.md does not start with the expected title")
    body = text[len(MARKER):]
    MCP_README.write_text(NOTE + body, encoding="utf-8", newline="\n")
    written = MCP_README.read_text(encoding="utf-8")
    assert written == NOTE + body
    print("MCP/README.md synced (%d bytes, %d lines)" % (len(written.encode()), len(written.splitlines())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
