"""Diagnostic: which part of spawning a piped child process is denied here?"""

from __future__ import annotations

import subprocess
import sys
import traceback


def attempt(label, fn):
    try:
        value = fn()
        print("DIAG::%s::OK::%r" % (label, value))
    except Exception as exc:  # noqa: BLE001
        print("DIAG::%s::FAIL::%s: %s" % (label, type(exc).__name__, exc))
        traceback.print_exc()


def main() -> int:
    py = sys.executable

    attempt("run_capture_output", lambda: subprocess.run(
        [py, "-c", "print('hello')"], capture_output=True, text=True, timeout=30).stdout.strip())

    def popen_pipes():
        proc = subprocess.Popen([py, "-c", "import sys; sys.stdout.write('x')"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=30)
        return out

    attempt("popen_pipes", popen_pipes)

    def popen_pipes_env():
        import os
        env = dict(os.environ)
        env["PROBE"] = "1"
        proc = subprocess.Popen([py, "-c", "print('y')"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        out, _ = proc.communicate(timeout=30)
        return out

    attempt("popen_pipes_env", popen_pipes_env)

    def anyio_spawn():
        import anyio

        async def run():
            async with await anyio.open_process([py, "-c", "print('z')"],
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE) as proc:
                data = b""
                async for chunk in proc.stdout:
                    data += chunk
                await proc.wait()
                return data

        return anyio.run(run)

    attempt("anyio_open_process", anyio_spawn)

    def mcp_stdio_client_spawn():
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=py, args=["-c", "print('server')"])

        async def run():
            async with stdio_client(params) as (read, write):
                return "opened"

        return anyio.run(run)

    attempt("mcp_stdio_client_open", mcp_stdio_client_spawn)

    def tempdir_probe():
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            return tmp

    attempt("tempdir", tempdir_probe)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
