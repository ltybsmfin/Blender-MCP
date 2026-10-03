#!/usr/bin/env python
"""Blender MCP server - stdio transport.

Run it with the self-contained virtual environment created for this project::

    <workspace>\\MCP\\.venv\\Scripts\\python.exe <workspace>\\MCP\\server.py

Stdout is reserved for the MCP protocol; diagnostics go to stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable when this file is started by absolute path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from blender_mcp.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
