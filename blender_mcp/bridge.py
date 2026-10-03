"""Blocking, single-request bridge client for the Blender side.

Framing: one UTF-8 JSON object per ``\\n``-terminated line, in both directions,
with a hard byte cap on either direction.  Every request carries the shared
token; the token is never logged.

Deliberate design choices
-------------------------
* **No automatic replay.**  A timeout, a dropped connection or a garbage answer
  raises immediately.  Mutating commands are never resent, so a slow Blender
  cannot silently double-apply an edit.  The connection is thrown away and the
  next call reconnects from scratch.
* One request at a time per client (a lock serialises callers), which matches
  the Blender side executing commands on its single main thread.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

from . import config


class BridgeError(RuntimeError):
    """Base class for every bridge failure surfaced to MCP tools."""

    code = "bridge_error"

    def __init__(self, message: str, details=None):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_tool_message(self) -> str:
        if self.details:
            try:
                return "%s | details: %s" % (self.message, json.dumps(self.details, default=str))
            except (TypeError, ValueError):
                return self.message
        return self.message


class BridgeUnavailable(BridgeError):
    code = "bridge_unavailable"


class BridgeTimeout(BridgeError):
    code = "bridge_timeout"


class BridgeProtocolError(BridgeError):
    code = "bridge_protocol_error"


class BlenderCommandError(BridgeError):
    """Blender answered with ``ok: false`` (an explicit, expected failure)."""

    code = "blender_command_error"

    def __init__(self, command: str, code: str, message: str, details=None):
        super().__init__("%s (command %r)" % (message, command), details=details)
        self.command = command
        self.remote_code = code
        self.remote_message = message

    def as_tool_message(self) -> str:
        base = "%s: %s" % (self.remote_code, self.remote_message)
        if self.details:
            try:
                return "%s | details: %s" % (base, json.dumps(self.details, default=str))
            except (TypeError, ValueError):
                return base
        return base


class BridgeClient:
    """Owns one TCP connection to the Blender bridge."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        timeout: float | None = None,
        max_frame: int = config.MAX_FRAME_BYTES,
    ):
        self.host = host or config.bridge_host()
        self.port = int(port if port is not None else config.bridge_port())
        self._explicit_token = token
        self.timeout = float(timeout if timeout is not None else config.call_timeout())
        self.max_frame = max_frame
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._lock = threading.RLock()
        self.calls = 0
        self.last_command: str | None = None

    # -- plumbing --------------------------------------------------------- #
    @property
    def address(self) -> str:
        return "%s:%d" % (self.host, self.port)

    def _token(self) -> str:
        token = self._explicit_token or config.read_token() or config.ensure_token()
        if not token:
            raise BridgeUnavailable(
                "no bridge token available at %s; start Blender with "
                "MCP/blender/bootstrap.py (which creates it)" % config.token_path()
            )
        return token

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b""

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        try:
            sock = socket.create_connection((self.host, self.port), timeout=config.CONNECT_TIMEOUT)
        except OSError as exc:
            raise BridgeUnavailable(
                "cannot connect to the Blender bridge at %s: %s. Start Blender and run "
                "MCP/blender/bootstrap.py (or call blender_launch_blender)." % (self.address, exc),
                details={"address": self.address, "errno": getattr(exc, "errno", None)},
            ) from exc
        sock.settimeout(self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._sock = sock
        self._buffer = b""
        return sock

    def _read_line(self, sock: socket.socket, deadline: float | None = None) -> bytes:
        while b"\n" not in self._buffer:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeTimeout("Blender bridge request deadline expired; the request was not replayed")
                sock.settimeout(remaining)
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                raise BridgeTimeout(
                    "Blender bridge at %s did not answer within %.1fs; the request was not replayed"
                    % (self.address, self.timeout),
                    details={"address": self.address, "timeout_s": self.timeout},
                ) from exc
            except OSError as exc:
                raise BridgeUnavailable("connection to %s failed: %s" % (self.address, exc)) from exc
            if not chunk:
                raise BridgeProtocolError(
                    "Blender bridge at %s closed the connection without answering" % self.address
                )
            self._buffer += chunk
            if len(self._buffer) > self.max_frame and b"\n" not in self._buffer:
                self.close()
                raise BridgeProtocolError(
                    "response from %s exceeds the %d byte frame cap" % (self.address, self.max_frame)
                )
        line, _, rest = self._buffer.partition(b"\n")
        self._buffer = rest
        if len(line) > self.max_frame:
            self.close()
            raise BridgeProtocolError(
                "answer line from %s is %d bytes, above the %d byte frame cap"
                % (self.address, len(line), self.max_frame)
            )
        return line

    # -- public API ------------------------------------------------------- #
    def call(self, command: str, params: dict | None = None, timeout: float | None = None) -> dict:
        """Send one command and return its result. Never retries, never replays."""
        params = params or {}
        payload = {
            "id": uuid.uuid4().hex,
            "token": self._token(),
            "command": command,
            "params": params,
        }
        try:
            data = json.dumps(payload, default=str).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise BridgeProtocolError("request for %r is not JSON serialisable: %s" % (command, exc)) from exc
        if len(data) > self.max_frame:
            raise BridgeProtocolError(
                "request for %r is %d bytes, above the %d byte frame cap" % (command, len(data), self.max_frame)
            )

        with self._lock:
            sock = self._connect()
            previous_timeout = sock.gettimeout()
            request_timeout = float(timeout if timeout is not None else self.timeout)
            deadline = time.monotonic() + request_timeout
            sock.settimeout(request_timeout)
            try:
                self.calls += 1
                self.last_command = command
                sock.sendall(data)
                raw = self._read_line(sock, deadline=deadline)
            except BridgeTimeout:
                # Release the connection and drop any partially read frame: a late
                # answer must never be mistaken for the next request's answer, and
                # the timed-out request is NOT replayed.
                self.close()
                raise
            except (BridgeUnavailable, BridgeProtocolError):
                self.close()
                raise
            except socket.timeout as exc:
                self.close()
                raise BridgeTimeout("Blender bridge write timed out; the request was not replayed") from exc
            except OSError as exc:
                self.close()
                raise BridgeUnavailable("Blender bridge transport failed: %s" % exc) from exc
            finally:
                if self._sock is sock:
                    try:
                        sock.settimeout(previous_timeout)
                    except OSError:
                        pass

        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.close()
            raise BridgeProtocolError(
                "answer for %r is not valid UTF-8 JSON: %s" % (command, exc)
            ) from exc
        if not isinstance(response, dict):
            self.close()
            raise BridgeProtocolError("answer for %r is not a JSON object" % command)
        if response.get("id") != payload["id"]:
            self.close()
            raise BridgeProtocolError(
                "answer id mismatch for %r (expected %s, got %r)"
                % (command, payload["id"], response.get("id"))
            )
        if response.get("ok"):
            return response.get("result") or {}
        error = response.get("error") or {}
        raise BlenderCommandError(
            command=command,
            code=str(error.get("code", "unknown_error")),
            message=str(error.get("message", "Blender reported an unspecified error")),
            details=error.get("details"),
        )

    def health(self, timeout: float | None = None) -> dict:
        return self.call("health", {}, timeout=timeout or 5.0)

    def is_healthy(self, timeout: float = 2.0) -> tuple[bool, dict | None]:
        try:
            return True, self.health(timeout=timeout)
        except BridgeError:
            return False, None
