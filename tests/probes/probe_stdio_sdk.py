"""Diagnostic: MCP SDK client session over stdlib pipes (no asyncio Proactor pipes)."""

from __future__ import annotations

import anyio
import inspect

from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

print("DIAG2::create_memory_object_stream_sig", inspect.signature(anyio.create_memory_object_stream))
print("DIAG2::client_session_sig", inspect.signature(ClientSession.__init__))
print("DIAG2::session_message_fields", sorted(SessionMessage.model_fields))
send, recv = anyio.create_memory_object_stream(10)
print("DIAG2::stream_types", type(send).__name__, type(recv).__name__)
msg = JSONRPCMessage.model_validate({"jsonrpc": "2.0", "id": 1, "method": "ping"})
print("DIAG2::roundtrip", SessionMessage(message=msg).message.model_dump_json(by_alias=True, exclude_none=True))
