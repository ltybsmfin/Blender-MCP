"""MCP stdio client transport built on stdlib pipes (test helper).

Why this exists
---------------
The MCP *stdio transport* means: the client writes newline-delimited JSON-RPC to
the server process' stdin and reads JSON-RPC from its stdout.  The official
``mcp.client.stdio.stdio_client`` implements that with ``anyio.open_process``,
which on Windows goes through asyncio's Proactor event loop and its
``asyncio.windows_utils.pipe()`` helper.  In this build environment the file
sandbox denies exactly that call (``PermissionError: [WinError 5]``), while
ordinary ``subprocess.Popen(stdin=PIPE, stdout=PIPE)`` works.

So this helper keeps the *official MCP client* - ``mcp.ClientSession`` - and the
real protocol, and only replaces the pipe plumbing: the child is started with
stdlib ``subprocess`` and two tasks shuttle bytes between the pipes and the
anyio memory-object streams that ``ClientSession`` reads from and writes to.
That is byte-for-byte the same framing the SDK uses
(``SessionMessage.message.model_dump_json(by_alias=True, exclude_none=True)`` +
``\\n``), so the protocol behaviour under test is unchanged.

On a normal machine ``stdio_client`` works, and the test can be switched with the
``BLENDER_MCP_FORCE_SDK_STDIO=1`` environment variable.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage


def _write_all(stream, data: bytes) -> None:
    stream.write(data)
    stream.flush()


async def _stdout_reader(process: subprocess.Popen, send) -> None:
    try:
        while True:
            line = await asyncio.to_thread(process.stdout.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith(b"content-length:"):  # defensive: never expected
                continue
            try:
                message = JSONRPCMessage.model_validate_json(line)
            except Exception as exc:  # noqa: BLE001 - surface protocol garbage to the session
                await send.send(exc)
                continue
            await send.send(SessionMessage(message=message))
    finally:
        with anyio.CancelScope(shield=True):
            await send.aclose()


async def _stdin_writer(process: subprocess.Popen, recv) -> None:
    try:
        async for session_message in recv:
            payload = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
            await asyncio.to_thread(_write_all, process.stdin, (payload + "\n").encode("utf-8"))
    except (BrokenPipeError, ValueError):
        return
    finally:
        with anyio.CancelScope(shield=True):
            await recv.aclose()


def _terminate(process: subprocess.Popen, timeout: float = 10.0) -> dict:
    """Close stdin, then wait/terminate/kill - only this process, bounded."""
    info = {"pid": process.pid}
    try:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
    except OSError:
        pass
    try:
        info["exit_code"] = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        info["terminate"] = True
        process.terminate()
        try:
            info["exit_code"] = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            info["kill"] = True
            process.kill()
            info["exit_code"] = process.wait(timeout=timeout)
    info["still_running"] = process.poll() is None
    return info


@asynccontextmanager
async def mcp_stdio_session(
    command: str | Path,
    args: list[str],
    env: dict | None = None,
    stderr_path: Path | None = None,
    cwd: Path | None = None,
    message_handler=None,
    initialization_callback=None,
):
    """Yield an initialised ``ClientSession`` talking to a real server process."""
    if os.environ.get("BLENDER_MCP_FORCE_SDK_STDIO") == "1":  # pragma: no cover - platform path
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=str(command), args=list(args), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, message_handler=message_handler) as session:
                initialized = await session.initialize()
                if initialization_callback:
                    initialization_callback(initialized)
                yield session, None
        return

    read_send, read_recv = anyio.create_memory_object_stream(100)
    write_send, write_recv = anyio.create_memory_object_stream(100)

    stderr_handle = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
    try:
        process = subprocess.Popen(  # noqa: S603 - explicit interpreter chosen by the caller
            [str(command), *[str(a) for a in args]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            env=env,
            cwd=str(cwd) if cwd else None,
            bufsize=0,
        )
    except OSError:
        if stderr_path:
            stderr_handle.close()
        raise

    teardown_info: dict = {}
    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_stdout_reader, process, read_send)
            task_group.start_soon(_stdin_writer, process, write_recv)
            try:
                async with ClientSession(read_recv, write_send, message_handler=message_handler) as session:
                    initialized = await session.initialize()
                    if initialization_callback:
                        initialization_callback(initialized)
                    yield session, process
            finally:
                await read_recv.aclose()
                await write_send.aclose()
                task_group.cancel_scope.cancel()
    finally:
        teardown_info = _terminate(process)
        # The server has exited, so no pipe reader can still wait on its output.
        # Close our parent handles explicitly instead of relying on GC.
        for handle in (process.stdin, process.stdout):
            if handle is not None and not handle.closed:
                handle.close()
        if stderr_path:
            stderr_handle.close()
    _LAST_TEARDOWN.clear()
    _LAST_TEARDOWN.update(teardown_info)


_LAST_TEARDOWN: dict = {}


def last_teardown() -> dict:
    """Teardown info of the most recently closed session (for reports)."""
    return dict(_LAST_TEARDOWN)
