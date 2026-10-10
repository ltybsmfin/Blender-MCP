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
import functools
import math
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .. import config
from ..bridge import BridgeClient
from ._startup import assert_launch_idle, probe_bridge


_LAUNCH_LOCK = threading.Lock()


def _serialized_launch(func):
    @functools.wraps(func)
    def guarded(*args, **kwargs):
        if not _LAUNCH_LOCK.acquire(blocking=False):
            raise ToolError("launch_in_progress: wait for the current startup; nothing else launched")
        try:
            return func(*args, **kwargs)
        finally:
            _LAUNCH_LOCK.release()
    return guarded


def _safe_extra_args(args: list[str] | None) -> list[str]:
    # Extra options are after Blender's '--', hence they are bootstrap options.
    # Never let them override the host, port, token path or disable the timer.
    values = list(args or [])
    index = 0
    while index < len(values):
        if values[index] == "--mcp-quiet":
            index += 1
        elif values[index] == "--mcp-pump-seconds" and index + 1 < len(values):
            try:
                seconds = float(values[index + 1])
            except ValueError as exc:
                raise ToolError("invalid_argument: pump duration must be a finite positive number") from exc
            if not math.isfinite(seconds) or seconds <= 0:
                raise ToolError("invalid_argument: pump duration must be a finite positive number")
            index += 2
        else:
            raise ToolError("invalid_argument: extra_args supports only --mcp-quiet or --mcp-pump-seconds N; bridge configuration overrides are rejected")
    return values


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_launch_blender",
        description=(
            "Launch the bundled Blender with the bridge bootstrap script and wait (bounded) until the "
            "bridge answers, returning its PID. If a healthy bridge already answers, "
            "nothing is launched and 'reused' is true. gui=true starts a normal GUI Blender (default; "
            "user preferences are kept, no --factory-startup); background=true starts a headless Blender "
            "with the manual socket pump (required, since timers never fire with --background), which is "
            "the right choice for automated tests. factory_startup=true is only for controlled test runs. "
            "The child process output is written to MCP/.runtime/logs/, never to the MCP stdio stream, and "
            "no existing Blender process is ever terminated. Startup is refused when Blender is already "
            "open without a bridge, the port is occupied, or health fails with authentication/protocol/timeout "
            "errors. extra_args permits only --mcp-quiet and --mcp-pump-seconds N."
        ),
    )
    @_serialized_launch
    def blender_launch_blender(
        gui: bool = True,
        background: bool = False,
        factory_startup: bool = False,
        wait_seconds: float = 30.0,
        reuse_existing: bool = True,
        blender_path: str | None = None,
        extra_args: list[str] | None = None,
    ) -> dict:
        if not math.isfinite(wait_seconds) or not 1 <= wait_seconds <= 60:
            raise ToolError("invalid_argument: wait_seconds must be between 1 and 60")
        additional_args = _safe_extra_args(extra_args)
        diagnosis = probe_bridge(bridge, timeout=2.0)
        if diagnosis["reachable"] and reuse_existing:
            health = diagnosis["health"]
            return {
                "launched": False,
                "reused": True,
                "reason": "a healthy Blender bridge already answers",
                "address": bridge.address,
                "blender_version": (health or {}).get("blender_version"),
                "pid": (health or {}).get("pid"),
            }
        assert_launch_idle(bridge, diagnosis)
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
            # A bootstrap exception must be an observable startup failure.
            args += ["--python-exit-code", "1"]
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
        args += additional_args

        creationflags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags = (subprocess.CREATE_NO_WINDOW if background else subprocess.DETACHED_PROCESS) | subprocess.CREATE_NEW_PROCESS_GROUP
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

        deadline = time.monotonic() + float(wait_seconds)
        health = None
        last_probe = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            last_probe = probe_bridge(bridge, timeout=1.5)
            if last_probe["reachable"]:
                health = last_probe["health"]
                if health.get("pid") != process.pid:
                    raise ToolError("launch_identity_mismatch: another bridge answered; started PID=%s, answering PID=%s. No process terminated; inspect %s" % (process.pid, health.get("pid"), log_path))
                break
            time.sleep(0.5)
        if health is None:
            raise ToolError(
                "bridge_start_failed: started PID=%s, exit_code=%s, bridge not ready on %s; "
                "last_stage=%s, last_error=%s, log=%s. The process was not terminated. "
                "Inspect this PID/log and retry health before any further launch."
                % (process.pid, process.poll(), bridge.address, (last_probe or {}).get("stage"),
                   (last_probe or {}).get("error_code"), log_path)
            )
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
