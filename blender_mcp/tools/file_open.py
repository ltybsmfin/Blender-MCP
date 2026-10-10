"""Named opening of an existing ``.blend`` inside ``<workspace>/Project``.

The typed MCP tool ``blender_open_blend`` is a thin, strict front end for the
bridge command ``open_blend`` (see ``blender/file_open.py``).  It never executes
anything itself: no path or file is created, the target is never written, no
Blender is started or stopped, and the ``BridgeClient`` gains no generic retry.

Two independent safety layers
-----------------------------
* **Here (before Blender is contacted).** ``path`` must be a normalized,
  *absolute* ``.blend`` path inside the ``Project`` root.  Relative paths, UNC
  and ``\\\\?\\``/``\\\\.\\`` device paths, NUL characters, ``.``/``..``/duplicate
  or trailing separators, paths that escape ``Project``, symlink/junction
  components, missing files, non-files, wrong extensions and unreadable files
  are rejected - through the same ``config.resolve_project_path`` route the
  other tools use - and a rejected call produces **no** bridge traffic at all.
* **In Blender (before anything is replaced).** The command repeats those checks
  and additionally proves the file is readable and parseable read-only.

After a successful ``open_blend`` reply the tool verifies the result with
**read-only** bridge commands only (``health`` and ``list_commands``, bounded by
:data:`VERIFY_BUDGET_SECONDS`): the loaded filepath must be the requested one,
the PID must be the one the reply reported, and ``open_blend`` must appear in the
running add-on's command roster.  The deadline is checked *before* every read and
each read is clamped to ``min(VERIFY_CALL_TIMEOUT, remaining)``; a new TCP
connection still uses ``config.CONNECT_TIMEOUT`` from ``bridge.py`` (which is not
modified), so this is a bound on the reads rather than a hard wall-clock cap.

A lost reply (transport timeout/disconnect) is handled with the same read-only
verification, but a matching filepath is **not** accepted as proof that *this*
call performed the load: a ``command_end`` record written by the Blender side
(``source == "bridge"``) with this call's request id must itself confirm
``opened``, the target path, ``use_scripts == false`` and the requested
``load_ui``.  Without such evidence the call fails with ``load_pending`` or
``verification_failed``.  ``open_blend`` itself is never re-sent, and only the
read-only commands are retried.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from .. import audit, config
from ..bridge import BlenderCommandError, BridgeClient, BridgeError
from ._common import START_HINT

#: The only command this module drives.  Never replayed after a lost reply.
OPEN_BLEND_COMMAND = "open_blend"
#: Read-only commands used for verification; these may be retried inside a budget.
HEALTH_COMMAND = "health"
LIST_COMMANDS_COMMAND = "list_commands"
#: Longest accepted path (Windows MAX_PATH headroom).
MAX_PATH_CHARS = 4096
#: Bounded budget for the mutating open request itself.  Loading a large file
#: blocks Blender's main thread, so this is deliberately more generous than the
#: default call timeout - but it is a bound, not an unbounded wait.
OPEN_CALL_TIMEOUT = 120.0
#: Total budget for the read-only post-open verification (configurable constant).
VERIFY_BUDGET_SECONDS = 10.0
#: Per-request timeout used inside that budget.
VERIFY_CALL_TIMEOUT = 3.0
#: Delay between verification attempts - never a busy loop.
VERIFY_POLL_INTERVAL = 0.25
#: Bounded number of audit events scanned when looking for request evidence.
EVIDENCE_SCAN_LIMIT = 2000

#: Booleans are validated in strict mode: the strings "true"/"false" and the
#: numbers 1/0 are rejected by the schema instead of being coerced.
StrictBool = Annotated[bool, Field(strict=True)]

TOOL_DESCRIPTION = (
    "Open an existing .blend inside <workspace>/Project as the session file of this Blender (bridge "
    "command open_blend; bpy.ops.wm.open_mainfile with EXEC_DEFAULT, use_scripts always false). It "
    "REPLACES the session: confirm_replace_current must be the strict JSON boolean true, "
    "otherwise the call fails with unsaved_changes (dirty) or confirmation_required (clean/unknown), "
    "because bpy.data.is_dirty is only a hint. 'path' must be a normalized ABSOLUTE .blend path with "
    "drive+root inside the project root; relative, drive-relative, rooted-without-drive, "
    "UNC/device, dot/dotdot, symlink/junction, outside-Project, missing, directory, wrong-extension "
    "and unreadable paths are refused before Blender and again inside it. The file is proven readable "
    "and parseable read-only first (a .blend suffix is never enough). The current file is never saved "
    "implicitly, the target never written, no second Blender started, and a failed load returns "
    "load_failed with honest state, never a rollback promise. load_ui defaults to false (layout kept). "
    "After success the result is re-verified read-only via health + list_commands in a bounded budget "
    "(reconnect allowed, open_blend never re-sent); a lost reply needs request-correlated bridge audit "
    "evidence, else load_pending/verification_failed. Details: docs/OPEN_BLEND.md."
)

_REMOTE_STAGES = {
    "unsaved_changes": "confirmation",
    "confirmation_required": "confirmation",
    "invalid_path": "path",
    "unc_path": "path",
    "device_path": "path",
    "path_outside_project": "path",
    "file_not_found": "path",
    "not_a_file": "path",
    "unreadable": "path",
    "invalid_project_dir": "path",
    "unsupported_api": "api",
    "invalid_blend": "preflight",
    "blend_parse_failed": "preflight",
    "preflight_changed_state": "preflight",
    "load_failed": "load",
}


def _tool_error(code: str, message: str, details: dict | None = None) -> ToolError:
    """One stable error shape: ``code: message | details: {...}``."""
    text = "%s: %s" % (code, message)
    if details:
        try:
            text += " | details: %s" % json.dumps(details, default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass
    return ToolError(text)


def _file_kind(path: Path) -> str:
    """``missing`` | ``file`` | ``other`` - the MCP-side existence probe."""
    try:
        if not path.exists():
            return "missing"
        return "file" if path.is_file() else "other"
    except OSError:
        return "missing"


def _probe_readable(path: Path) -> bool:
    """Actually open the file for reading; a permissions error is a refusal."""
    try:
        with open(str(path), "rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _same_path(left, right) -> bool:
    """True when both arguments name the same normalized location.

    Accepts ``str`` and ``os.PathLike`` because the tool compares its own
    :class:`~pathlib.Path` target against the strings Blender reports.
    """
    if left is None or right is None:
        return False
    try:
        left_text = os.fspath(left)
        right_text = os.fspath(right)
        if isinstance(left_text, bytes) or isinstance(right_text, bytes):
            return False
        if not left_text or not right_text:
            return False
        return os.path.normcase(os.path.realpath(left_text)) == os.path.normcase(os.path.realpath(right_text))
    except (OSError, TypeError, ValueError):
        return False


def _absolute_syntax_ok(text: str, windows: bool | None = None) -> bool:
    """True for an absolute path that carries both a root and (on Windows) a drive.

    ``os.path.isabs`` alone is not enough on Windows: ``'\\dir'`` (rooted, no
    drive) is reported as absolute and would be resolved against the *current
    drive* of whichever process reads it, and ``'C:foo'`` is drive-relative.
    UNC/device prefixes are not accepted either (the caller reports them with
    their own codes).  ``windows`` defaults to the running platform and exists so
    the platform rule set stays unit-testable for both platforms.
    """
    if text.startswith("\\\\") or text.startswith("//"):
        return False
    if windows is None:
        if not Path(text).is_absolute():
            return False
        windows = os.name == "nt"
    if windows:
        drive, tail = os.path.splitdrive(text)
        return bool(drive) and tail[:1] in ("\\", "/")
    return text.startswith("/")


def _resolve_target(path) -> Path:
    """Validate ``path`` and return the resolved absolute target, or raise ToolError."""
    if type(path) is not str:
        raise _tool_error("invalid_argument", "path must be a string, got %s" % type(path).__name__)
    raw = path.strip()
    if not raw:
        raise _tool_error("invalid_path", "path must not be empty")
    if "\x00" in raw:
        raise _tool_error("invalid_path", "path must not contain a NUL character")
    if len(raw) > MAX_PATH_CHARS:
        raise _tool_error("invalid_path", "path is longer than %d characters" % MAX_PATH_CHARS,
                          {"length": len(raw)})
    if raw.startswith("\\\\") or raw.startswith("//"):
        kind = "device_path" if raw[2:4] in ("?\\", ".\\", "?/", "./") else "unc_path"
        raise _tool_error(
            kind,
            "UNC and device paths are rejected; the file must live inside <workspace>/Project",
            {"path": raw},
        )
    sealed = raw.replace("/", os.sep)
    normalized = os.path.normpath(sealed)
    if normalized != sealed:
        raise _tool_error(
            "invalid_path",
            "path must be a normalized absolute path without '.', '..', duplicate or trailing separators",
            {"path": raw, "normalized": normalized},
        )
    if not _absolute_syntax_ok(normalized):
        raise _tool_error(
            "invalid_path",
            "path must be an absolute path with a drive and root inside <workspace>/Project "
            "(drive-relative 'C:foo' and rooted-without-drive '\\dir' are rejected)",
            {"path": raw},
        )
    try:
        root = config.project_dir()
    except config.PathValidationError as exc:
        raise _tool_error("invalid_project_dir", str(exc)) from exc
    try:
        relative = os.path.relpath(normalized, str(root))
    except ValueError as exc:
        # Windows raises ValueError for paths on two different volumes; that is a
        # containment failure, not an internal error.
        raise _tool_error(
            "path_outside_project",
            "path is on a different volume than <workspace>/Project",
            {"path": raw, "resolved": normalized, "project_root": str(root),
             "reason": "different_drive", "message": str(exc)},
        ) from exc
    if relative == ".." or relative.startswith(".." + os.sep) or os.path.isabs(relative):
        raise _tool_error(
            "path_outside_project",
            "path must stay inside <workspace>/Project",
            {"path": raw, "resolved": normalized, "project_root": str(root)},
        )
    try:
        target = config.resolve_project_path(relative)
    except config.PathValidationError as exc:
        message = str(exc)
        code = "path_outside_project" if ("outside" in message or "escapes" in message) else "invalid_path"
        raise _tool_error(code, message, {"path": raw}) from exc
    kind = _file_kind(target)
    if kind == "missing":
        if target.suffix.lower() != ".blend":
            raise _tool_error("invalid_path", "only .blend files can be opened, got %r" % target.name,
                              {"path": str(target)})
        raise _tool_error("file_not_found", "no file at %s" % target,
                          {"path": str(target), "project_root": str(root)})
    if kind != "file":
        raise _tool_error("not_a_file", "%s is not a regular file" % target, {"path": str(target)})
    if target.suffix.lower() != ".blend":
        raise _tool_error("invalid_path", "only .blend files can be opened, got %r" % target.name,
                          {"path": str(target)})
    if not _probe_readable(target):
        raise _tool_error("unreadable", "cannot read %s" % target, {"path": str(target)})
    return target


# --------------------------------------------------------------------------- #
# bounded, read-only verification
# --------------------------------------------------------------------------- #
def _read_health(bridge: BridgeClient, budget: float):
    """One read, never allowed to outlive the remaining verification budget."""
    timeout = min(VERIFY_CALL_TIMEOUT, budget)
    try:
        payload = bridge.call(HEALTH_COMMAND, {}, timeout=timeout)
    except BridgeError as exc:
        return None, {"command": HEALTH_COMMAND, "code": getattr(exc, "code", type(exc).__name__),
                      "message": str(exc), "timeout_s": timeout}
    if not isinstance(payload, dict):
        return None, {"command": HEALTH_COMMAND, "code": "bridge_protocol_error",
                      "message": "the health payload is not a JSON object", "timeout_s": timeout}
    return payload, None


def _read_command_names(bridge: BridgeClient, budget: float):
    """One read, never allowed to outlive the remaining verification budget."""
    timeout = min(VERIFY_CALL_TIMEOUT, budget)
    try:
        payload = bridge.call(LIST_COMMANDS_COMMAND, {}, timeout=timeout)
    except BridgeError as exc:
        return None, {"command": LIST_COMMANDS_COMMAND, "code": getattr(exc, "code", type(exc).__name__),
                      "message": str(exc), "timeout_s": timeout}
    rows = payload.get("commands") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None, {"command": LIST_COMMANDS_COMMAND, "code": "bridge_protocol_error",
                      "message": "list_commands did not return a command list", "timeout_s": timeout}
    names = {str(row.get("name")) for row in rows if isinstance(row, dict)}
    return names, None


def _bounded_verify(bridge: BridgeClient, target: Path, expected_pid, started: float, mode: str) -> dict:
    """Retry only ``health``/``list_commands`` inside a bounded budget.

    The deadline is honoured *before* every read: no RPC is started once the
    budget is spent, and each read gets ``min(VERIFY_CALL_TIMEOUT, remaining)``
    so a single call can never outlive the budget.  A TCP *connect* inside
    ``bridge.call`` is still governed by ``config.CONNECT_TIMEOUT`` (bridge.py
    itself stays untouched), so the wall clock can exceed the budget by at most
    that connect timeout - this is a bound on the reads, not a hard wall-clock
    cap.  The mutating ``open_blend`` command is never re-sent; the socket is
    dropped between attempts (the client reconnects on the next read) because a
    stale connection is the usual reason a read fails right after a switch.
    """
    deadline = time.monotonic() + VERIFY_BUDGET_SECONDS
    attempts = 0
    reads_issued = 0
    reads_skipped = 0
    report: dict = {"stage": "not_attempted", "verified": False, "mode": mode}

    def remaining() -> float:
        return deadline - time.monotonic()

    while True:
        budget = remaining()
        if budget <= 0:
            reads_skipped += 1
            break
        attempts += 1
        reads_issued += 1
        health, health_error = _read_health(bridge, budget)
        if health is None:
            report = {"stage": "health_unreachable", "health_error": health_error,
                      "verified": False, "mode": mode}
        else:
            budget = remaining()
            if budget <= 0:
                reads_skipped += 1
                names, commands_error = None, {
                    "command": LIST_COMMANDS_COMMAND, "code": "deadline_exhausted",
                    "message": "the verification budget expired before list_commands could be read",
                    "timeout_s": 0.0}
            else:
                reads_issued += 1
                names, commands_error = _read_command_names(bridge, budget)
            listed = names is not None and OPEN_BLEND_COMMAND in names
            path_ok = _same_path(health.get("blend_filepath"), target)
            pid_ok = expected_pid is not None and health.get("pid") == expected_pid
            report = {
                "stage": "verified" if (path_ok and pid_ok and listed) else "mismatch",
                "health_blend_filepath": health.get("blend_filepath"),
                "health_blender_version": health.get("blender_version"),
                "health_scene": health.get("scene"),
                "health_background": health.get("background"),
                "expected_path": str(target),
                "path_matches": path_ok,
                "health_pid": health.get("pid"),
                "expected_pid": expected_pid,
                "pid_matches": pid_ok,
                "open_blend_listed": listed,
                "list_commands_error": commands_error,
                "verified": bool(path_ok and pid_ok and listed),
                "mode": mode,
            }
            if report["verified"]:
                break
        budget = remaining()
        if budget <= 0:
            break
        try:
            bridge.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
        time.sleep(min(VERIFY_POLL_INTERVAL, budget))
    report["attempts"] = attempts
    report["deadline_s"] = VERIFY_BUDGET_SECONDS
    report["deadline_exhausted"] = not report.get("verified", False)
    report["reads_issued"] = reads_issued
    report["reads_skipped_for_deadline"] = reads_skipped
    report["per_call_timeout_s"] = VERIFY_CALL_TIMEOUT
    report["connection_setup_note"] = (
        "each read is clamped to the remaining budget; a new TCP connect inside the client uses "
        "config.CONNECT_TIMEOUT and is budgeted separately"
    )
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    report["commands_retried"] = [HEALTH_COMMAND, LIST_COMMANDS_COMMAND]
    report["open_blend_replayed"] = False
    return report


def _audit_evidence(request_id, target: Path, load_ui: bool) -> dict:
    """Request-correlated proof that *this* call's ``open_blend`` reached Blender.

    Both halves of the bridge write JSONL audit files into the same directory and
    share the request id of the MCP call, so a command that was executed even
    though its reply was lost still leaves a ``command_end`` record.  Only the
    **Blender side** (``source == "bridge"``) counts, and the recorded result must
    itself confirm the load and the safety options (``opened``, the target path,
    ``use_scripts == False`` and the requested ``load_ui``) - a merely similar
    path or another writer is never accepted as proof.
    """
    if not request_id:
        return {"found": False, "reason": "no_request_id"}
    try:
        events = audit.tail_events(limit=EVIDENCE_SCAN_LIMIT)
    except Exception as exc:  # noqa: BLE001 - evidence gathering must never raise
        return {"found": False, "reason": "audit_unreadable",
                "error": "%s: %s" % (type(exc).__name__, exc)}
    matches = [
        entry for entry in events
        if isinstance(entry, dict)
        and entry.get("event") == "command_end"
        and str(entry.get("source") or "") == "bridge"
        and entry.get("command") == OPEN_BLEND_COMMAND
        and str(entry.get("request_id") or "") == str(request_id)
    ]
    if not matches:
        return {"found": False, "reason": "no_record", "request_id": str(request_id),
                "scanned_events": len(events)}
    entry = matches[-1]
    result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
    recorded = result.get("path")
    confirmation = {
        "ok": entry.get("ok"),
        "opened": result.get("opened"),
        "path_matches": _same_path(recorded, target),
        "use_scripts": result.get("use_scripts"),
        "use_scripts_false": result.get("use_scripts") is False,
        "load_ui": result.get("load_ui"),
        "load_ui_matches_request": result.get("load_ui") == load_ui,
    }
    return {
        "found": True,
        "confirmed": bool(
            confirmation["ok"] is True
            and confirmation["opened"] is True
            and confirmation["path_matches"]
            and confirmation["use_scripts_false"]
            and confirmation["load_ui_matches_request"]
        ),
        "request_id": str(request_id),
        "event": "command_end",
        "source": entry.get("source"),
        "pid": entry.get("pid"),
        "session": entry.get("session"),
        "ts": entry.get("ts"),
        "ok": entry.get("ok"),
        "code": entry.get("code"),
        "recorded_path": recorded,
        "recorded_path_matches": confirmation["path_matches"],
        "confirmation": confirmation,
        "scanned_events": len(events),
        "duplicate_records": len(matches) - 1,
    }


def _verify_without_reply(bridge: BridgeClient, target: Path, exc: BridgeError, request_id,
                          load_ui: bool, started: float) -> dict:
    """A lost reply is verified read-only - never by re-sending the open."""
    evidence = _audit_evidence(request_id, target, load_ui)
    transport_error = {"command": OPEN_BLEND_COMMAND, "code": getattr(exc, "code", type(exc).__name__),
                       "message": str(exc)}
    if evidence.get("found") and evidence.get("ok") is not True:
        raise _tool_error(
            "load_failed",
            "open_blend failed inside Blender: the reply was lost (%s), but the bridge audit record for "
            "this request reports the failure %r. The command was NOT re-sent."
            % (transport_error["code"], evidence.get("code")),
            {"request_evidence": evidence, "transport_error": transport_error},
        )
    usable = bool(evidence.get("found") and evidence.get("confirmed"))
    expected_pid = evidence.get("pid") if usable else None
    report = _bounded_verify(bridge, target, expected_pid, started, mode="post_hoc")
    report["request_evidence"] = evidence
    report["request_evidence_usable"] = usable
    report["transport_error"] = transport_error
    if report.get("verified") and usable:
        recorded = evidence.get("confirmation") or {}
        return {
            "opened": True,
            "path": str(target),
            "current_filepath": report.get("health_blend_filepath"),
            "current_scene": report.get("health_scene"),
            "pid": report.get("health_pid"),
            "blender_version": report.get("health_blender_version"),
            "load_ui": bool(load_ui),
            "use_scripts": False,
            "ui_mode": {"background": report.get("health_background"), "load_ui": bool(load_ui)},
            "previous": None,
            "previous_unavailable": "the open_blend reply was lost, so the pre-open state was not observed",
            "verified_after_transport_failure": True,
            "confirmed_by": "request-correlated bridge audit command_end (source=bridge) + read-only "
                            "health/list_commands",
            "safety_options_confirmed_by_evidence": {
                "use_scripts_false": recorded.get("use_scripts") is False,
                "load_ui": recorded.get("load_ui"),
                "opened": recorded.get("opened"),
                "path_matches": recorded.get("path_matches"),
            },
            "target_written": False,
            "saved_before_open": False,
            "rollback": "not_attempted",
            "transport_error": transport_error,
            "verification": report,
            "note": "the open_blend reply was lost in transport; completion is proven by the Blender-side "
                    "audit record carrying this MCP request id (same PID and loaded filepath, "
                    "use_scripts=false and the requested load_ui) plus the read-only health and "
                    "command-roster check. The open was not replayed and the live file was not re-opened "
                    "by this tool.",
        }
    if report.get("path_matches") is True:
        raise _tool_error(
            "load_pending",
            "%s is the loaded file now, but this call has no usable request-correlated bridge audit "
            "evidence (source=bridge, same request id, opened/path/use_scripts/load_ui confirmed) that "
            "ITS open_blend performed the load (reply lost: %s). A matching path alone is not accepted "
            "as proof - re-check with blender_health and do not re-send the open blindly."
            % (target, transport_error["code"]),
            report,
        )
    raise _tool_error(
        "verification_failed",
        "the open_blend reply was lost (%s) and the read-only verification could not confirm that this "
        "call loaded %s; the command was not re-sent. Nothing is reported as success.%s"
        % (transport_error["code"], target,
           " %s" % START_HINT if report.get("stage") == "health_unreachable" else ""),
        report,
    )


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(name="blender_open_blend", description=TOOL_DESCRIPTION)
    def blender_open_blend(
        path: str,
        confirm_replace_current: StrictBool = False,
        load_ui: StrictBool = False,
    ) -> dict:
        if type(confirm_replace_current) is not bool:
            raise _tool_error(
                "invalid_argument",
                "confirm_replace_current must be a boolean, got %s" % type(confirm_replace_current).__name__,
            )
        if type(load_ui) is not bool:
            raise _tool_error("invalid_argument", "load_ui must be a boolean, got %s" % type(load_ui).__name__)
        target = _resolve_target(path)
        request_id = audit.current_request_id()
        started = time.perf_counter()
        params = {
            "path": str(target),
            "confirm_replace_current": confirm_replace_current,
            "load_ui": load_ui,
        }
        try:
            reply = bridge.call(OPEN_BLEND_COMMAND, params, timeout=OPEN_CALL_TIMEOUT)
        except BlenderCommandError as exc:
            stage = _REMOTE_STAGES.get(exc.remote_code, "command")
            raise ToolError("%s | stage: %s" % (exc.as_tool_message(), stage)) from exc
        except BridgeError as exc:
            return _verify_without_reply(bridge, target, exc, request_id, load_ui, started)
        if not isinstance(reply, dict) or reply.get("opened") is not True:
            raise _tool_error(
                "load_failed",
                "the bridge answered open_blend without a positive 'opened' payload; nothing is reported "
                "as success",
                {"reply": reply if isinstance(reply, dict) else {"type": type(reply).__name__}},
            )
        if not _same_path(reply.get("path"), target):
            raise _tool_error(
                "verification_failed",
                "the bridge reported success for %s but its payload names %r as the opened file"
                % (target, reply.get("path")),
                {"stage": "reply_path_mismatch", "reply_path": reply.get("path"), "expected_path": str(target)},
            )
        report = _bounded_verify(bridge, target, reply.get("pid"), started, mode="reply")
        if not report.get("verified"):
            hint = " %s" % START_HINT if report.get("stage") == "health_unreachable" else ""
            raise _tool_error(
                "verification_failed",
                "open_blend reported success but the read-only verification (health + list_commands, "
                "bounded to %.1fs) did not confirm it: the loaded filepath, the bridge PID or the command "
                "roster did not match. The open was NOT re-sent; inspect blender_health before doing "
                "anything else.%s" % (VERIFY_BUDGET_SECONDS, hint),
                report,
            )
        result = dict(reply)
        result["verification"] = report
        return result
