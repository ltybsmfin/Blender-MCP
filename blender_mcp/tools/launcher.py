"""Optional helper tool that launches Blender with the bridge enabled.

Design constraints honoured here:

* **Reuse first.**  If a healthy bridge already answers, nothing is launched.
* **Never kill anything.**  This tool only starts a process; it never terminates
  Blender instances it did not start.  The launched process handle is kept so the
  MCP server can report it, but no kill/stop tool is exposed.
* **GUI by default, headless on request.**  ``--factory-startup`` is opt-in: a
  normal user launch keeps the user's preferences.  Headless launches must pump
  the socket loop manually (``--mcp-pump``) because ``bpy.app.timers`` does not
  fire in ``--background``.
* **stdout stays clean.**  The child's stdout/stderr never touch the MCP stdio
  stream; they go to a log file under ``MCP/.runtime/logs``.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .. import config
from ..bridge import BridgeClient


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_launch_blender",
        description=(
            "Launch the bundled Blender with the bridge bootstrap script. Returns immediately with the "
            "PID and waits (bounded) until the bridge answers. If a healthy bridge already answers, "
            "nothing is launched and 'reused' is true. gui=true starts a normal GUI Blender (default; "
            "user preferences are kept, no --factory-startup); background=true starts a headless Blender "
            "with the manual socket pump (required, since timers never fire with --background), which is "
            "the right choice for automated tests. factory_startup=true is only for controlled test runs. "
            "The child process output is written to MCP/.runtime/logs/, never to the MCP stdio stream, and "
            "no existing Blender process is ever terminated."
        ),
    )
    def blender_launch_blender(
        gui: bool = True,
        background: bool = False,
        factory_startup: bool = False,
        wait_seconds: float = 30.0,
        reuse_existing: bool = True,
        blender_path: str | None = None,
        extra_args: list[str] | None = None,
    ) -> dict:
        if reuse_existing:
            healthy, health = bridge.is_healthy(timeout=2.0)
            if healthy:
                return {
                    "launched": False,
                    "reused": True,
                    "reason": "a healthy Blender bridge already answers",
                    "address": bridge.address,
                    "blender_version": (health or {}).get("blender_version"),
                    "pid": (health or {}).get("pid"),
                }
        try:
            executable = config.resolve_blender_executable(blender_path)
        except FileNotFoundError as exc:
            raise ToolError(str(exc)) from exc

        if not gui:
            background = True
        if background:
            gui = False
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = config.LOG_DIR / ("blender_launch_%d.log" % int(time.time()))
        bootstrap = Path(config.BOOTSTRAP_PATH)
        if not bootstrap.is_file():
            raise ToolError("bootstrap script missing: %s" % bootstrap)

        args = [str(executable)]
        if background:
            args.append("--background")
        if factory_startup:
            args.append("--factory-startup")
        args += [
            "--python",
            str(bootstrap),
            "--",
            "--mcp-host",
            config.bridge_host(),
            "--mcp-port",
            str(config.bridge_port()),
            "--mcp-token-file",
            str(config.token_path()),
            "--mcp-project-dir",
            str(config.project_dir()),
        ]
        if background:
            args.append("--mcp-pump")
        args += list(extra_args or [])

        creationflags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            with open(log_path, "ab") as log_file:
                process = subprocess.Popen(  # noqa: S603 - explicit, user-requested executable
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                    close_fds=True,
                )
        except OSError as exc:
            raise ToolError("could not start Blender (%s): %s" % (executable, exc)) from exc

        deadline = time.time() + max(1.0, min(float(wait_seconds), 300.0))
        health = None
        while time.time() < deadline:
            if process.poll() is not None:
                break
            healthy, payload = bridge.is_healthy(timeout=1.5)
            if healthy:
                health = payload
                break
            time.sleep(0.5)
        return {
            "launched": True,
            "reused": False,
            "pid": process.pid,
            "executable": str(executable),
            "mode": "background" if background else "gui",
            "factory_startup": factory_startup,
            "args": args,
            "log_file": str(log_path),
            "bridge_ready": health is not None,
            "health": health,
            "exited_early": process.poll() is not None and health is None,
            "exit_code": process.poll(),
            "note": (
                "the child's output is in the log file; the MCP stdio stream is never mixed with it"
            ),
        }
