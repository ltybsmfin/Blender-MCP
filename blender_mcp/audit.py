"""Persistent, bounded audit log for **every** MCP operation.

What is recorded
----------------
One JSON line (JSONL) per event, in ``<MCP>/.runtime/logs`` by default:

* ``session_start`` / ``session_stop`` - one pair per server / Blender process.
* ``tool_start`` / ``tool_end`` - the MCP tool call itself: tool name, the
  sanitised arguments, the outcome (``ok`` / ``error`` / ``rolled_back``), a
  bounded result summary, the error text and the wall-clock duration.
* ``bridge_ok`` / ``bridge_error`` - every command sent to the Blender bridge,
  with the same correlation id, so an MCP call and the Blender-side command it
  produced can be stitched together.
* ``batch_step`` / ``batch_rollback`` - the per-operation result of a batched
  graph edit and the transaction verdict.

Hard rules (they are enforced here, not by the callers)
------------------------------------------------------
* **No secrets, ever.** Keys that name a secret (``token``, ``secret``,
  ``password``, ``authorization``, ...) are replaced by ``***``, and the live
  bridge token is scrubbed out of every string as well.
* **Bounded.** Strings are capped, long arrays become a ``length`` + ``sha256``
  + small sample summary, base64/binary payloads become a length + hash, and the
  nesting depth and key count are capped. A 4 MiB vertex list can never blow the
  log up.
* **Append-only, one file per process.** ``audit-<source>-<pid>-<session>.jsonl``
  means two servers can never interleave a line inside the same file, so no
  cross-process lock is needed. Size rotation renames the file to a numbered
  archive in the same directory and starts a fresh one; the viewer keeps working
  across a rotation because it tracks offsets per path and re-reads a file whose
  size shrank.
* **Never fatal.** A logging failure prints one bounded warning on *stderr* (never
  stdout, which carries the MCP protocol) and is otherwise ignored - modelling
  must keep working when the log directory is read-only or full.

This module is standard-library only and never touches ``bpy``.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

# --------------------------------------------------------------------------- #
# constants / documented limits
# --------------------------------------------------------------------------- #
AUDIT_VERSION = 1

#: Version of the *add-on side* audit support. The MCP server only injects the
#: correlation field into a bridge request when the running Blender announces at
#: least this version in its ``health`` payload, so an older add-on keeps working.
ADDON_AUDIT_PROTOCOL = 1

#: Reserved per-request field. ``blender_mcp_addon.dispatch`` strips it before
#: validating the parameters, so it can never be mistaken for a command argument.
RESERVED_PARAM = "_mcp_audit"

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_KEEP_ARCHIVES = 5

MAX_STRING = 320                 # characters kept per string value
MAX_ITEMS = 20                   # array items kept before hashing/summarising
MAX_DICT_KEYS = 64               # keys kept per object
MAX_DEPTH = 5                    # nesting depth
MAX_HASH_ITEMS = 200_000         # above this an array is not hashed, only sized
MAX_TAIL_FILES = 40              # files scanned by the viewer / log tool
MAX_TAIL_BYTES_PER_FILE = 1_500_000
#: Hard cap on how many events one read may return. The MCP log *tool* clamps its
#: own ``limit`` to 500; the dashboard reads a wider window internally so it can
#: pair starts with ends before showing a slice.
MAX_EVENTS_READ = 5000
FILE_PREFIX = "audit-"

_SECRET_MARKERS = (
    "token", "secret", "password", "passwd", "credential", "authorization",
    "api_key", "apikey", "private_key", "bearer",
)
#: Keys that merely *mention* a secret without carrying one.
_SECRET_SAFE_SUFFIXES = ("_present", "_path", "_file", "_count", "_available", "_supported", "_protocol")
#: Keys whose value is a base64 payload rather than a number or a flag. Deliberately
#: narrow: a *byte count* field (``image_bytes``) must stay a number, while any
#: genuinely binary value (``bytes``/``bytearray``) is summarised by type anyway.
_BINARY_KEY_SUFFIXES = ("base64", "b64")
_TOKEN_PATTERN = re.compile(r"(?i)\b(token|secret|password|passwd|bearer|api[_-]?key)\b\s*[:=]\s*[^\s,;\"')}]+")


def now_iso() -> str:
    """UTC timestamp with millisecond resolution, e.g. ``2026-10-03T07:12:33.421Z``."""
    moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def audit_dir() -> Path:
    """The audit directory: an explicit ``configure(directory=...)`` wins over
    ``BLENDER_MCP_AUDIT_DIR``, which wins over ``<MCP>/.runtime/logs``."""
    if _DIR_OVERRIDE is not None:
        return _DIR_OVERRIDE
    override = os.environ.get("BLENDER_MCP_AUDIT_DIR")
    if override:
        return Path(override)
    return config.LOG_DIR


#: Set by an explicit ``configure(directory=...)`` (the server entry point and the
#: tests use it); kept separate so that a configured reader and writer always agree.
_DIR_OVERRIDE: Path | None = None


def is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    if lowered.endswith(_SECRET_SAFE_SUFFIXES):
        return False
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _is_binary_key(key: str) -> bool:
    return str(key).lower().endswith(_BINARY_KEY_SUFFIXES)


def sha256_short(value) -> str:
    """Short, stable digest of an arbitrary JSON-able value."""
    try:
        raw = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8", "replace")
    except (TypeError, ValueError):  # pragma: no cover - defensive
        raw = repr(value).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# sanitisation
# --------------------------------------------------------------------------- #
class Sanitizer:
    """Bounded, secret-free rendering of arbitrary arguments/results."""

    def __init__(self, token: str | None = None):
        self._token = token or ""

    def scrub_text(self, text: str) -> str:
        out = str(text)
        if self._token and self._token in out:
            out = out.replace(self._token, "***")
        out = _TOKEN_PATTERN.sub(lambda m: "%s=***" % m.group(1), out)
        return out

    def value(self, value, key: str | None = None, depth: int = 0):
        if key is not None:
            if is_secret_key(key):
                return "***"
            if _is_binary_key(key):
                length = None
                try:
                    length = len(value)
                except TypeError:
                    length = None
                return {"_type": "binary", "length": length, "sha256": sha256_short(value)}
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return str(value)
            return value
        if isinstance(value, str):
            text = self.scrub_text(value)
            if len(text) > MAX_STRING:
                return "%s…<+%d chars>" % (text[:MAX_STRING], len(text) - MAX_STRING)
            return text
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"_type": "bytes", "length": len(bytes(value)), "sha256": sha256_short(bytes(value))}
        if depth >= MAX_DEPTH:
            return {"_type": type(value).__name__, "truncated": "max_depth", "sha256": sha256_short(value)}
        if isinstance(value, dict):
            out: dict = {}
            items = list(value.items())
            for name, item in items[:MAX_DICT_KEYS]:
                out[str(name)] = self.value(item, key=str(name), depth=depth + 1)
            if len(items) > MAX_DICT_KEYS:
                out["_truncated_keys"] = len(items) - MAX_DICT_KEYS
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            total = len(items)
            if total > MAX_ITEMS:
                summary = {
                    "_type": "array",
                    "length": total,
                    "sample_size": MAX_ITEMS,
                    "sample": [self.value(item, depth=depth + 1) for item in items[:MAX_ITEMS]],
                    "truncated": total - MAX_ITEMS,
                }
                summary["sha256"] = sha256_short(items) if total <= MAX_HASH_ITEMS else None
                return summary
            return [self.value(item, depth=depth + 1) for item in items]
        # Anything else (bpy wrappers, dataclasses, ...) - keep it bounded and safe.
        try:
            return self.scrub_text(repr(value))[:MAX_STRING]
        except Exception:  # noqa: BLE001 - representation must never break logging
            return "<unrepresentable %s>" % type(value).__name__

    def fields(self, mapping: dict | None) -> dict:
        if not mapping:
            return {}
        return {str(k): self.value(v, key=str(k)) for k, v in list(mapping.items())[:MAX_DICT_KEYS]}


# --------------------------------------------------------------------------- #
# the logger
# --------------------------------------------------------------------------- #
class AuditLog:
    """One append-only JSONL file per process, with size rotation and archives."""

    def __init__(
        self,
        directory: Path | str | None = None,
        source: str = "mcp",
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
        keep_archives: int = DEFAULT_KEEP_ARCHIVES,
        session: str | None = None,
        token: str | None = None,
        enabled: bool = True,
    ):
        self.directory = Path(directory) if directory else audit_dir()
        self.source = str(source)
        self.max_bytes = max(4096, int(max_bytes))
        self.keep_archives = max(1, int(keep_archives))
        self.session = session or uuid.uuid4().hex[:12]
        self.pid = os.getpid()
        self.started_at = time.time()
        self.enabled = bool(enabled)
        if token is None:
            try:
                token = config.read_token() or ""
            except Exception:  # noqa: BLE001 - never fail because of the token file
                token = ""
        self.sanitizer = Sanitizer(token)
        self._seq = 0
        self._lock = threading.RLock()
        self._handle = None
        self._path: Path | None = None
        self._archive_index = 0
        self.failures = 0
        self._last_notice_at = 0.0
        self._thread_name = threading.current_thread().name

    # -- location ---------------------------------------------------------- #
    @property
    def name(self) -> str:
        return "%s%s-%d-%s.jsonl" % (FILE_PREFIX, self.source, self.pid, self.session)

    @property
    def path(self) -> Path:
        return self.directory / self.name

    def describe(self) -> dict:
        return {
            "directory": str(self.directory),
            "file": str(self.path),
            "source": self.source,
            "session": self.session,
            "pid": self.pid,
            "max_bytes": self.max_bytes,
            "keep_archives": self.keep_archives,
            "enabled": self.enabled,
            "events": self._seq,
            "failures": self.failures,
        }

    # -- failure reporting ------------------------------------------------- #
    def notice(self, message: str) -> None:
        """Bounded stderr warning; the MCP protocol owns stdout, so never write there."""
        now = time.time()
        with self._lock:
            if now - self._last_notice_at < 30.0:
                return
            self._last_notice_at = now
        try:
            sys.stderr.write("[blender-mcp] audit %s\n" % self.sanitizer.scrub_text(message))
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass

    def _fail(self, exc: Exception) -> None:
        self.failures += 1
        self.notice("log write failed (%d total): %s: %s" % (self.failures, type(exc).__name__, exc))

    # -- rotation ---------------------------------------------------------- #
    def _open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path = self.path
        self._handle = open(self._path, "a", encoding="utf-8", newline="\n")
        self._archive_index = _next_archive_index(self.directory, self.name)

    def _rotate(self) -> None:
        """Rename the live file to ``<name>.<NNN>.jsonl`` and start a new one."""
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        self._archive_index += 1
        archive = self.directory / ("%s.%03d.jsonl" % (self._path.stem, self._archive_index))
        try:
            if self._path.exists():
                os.replace(str(self._path), str(archive))
        except OSError:
            # Rotation failure must not stop logging: fall back to appending.
            pass
        self._prune_archives()
        self._open()

    def _prune_archives(self) -> None:
        archives = sorted(self.directory.glob("%s.*.jsonl" % self._path.stem))
        for stale in archives[: max(0, len(archives) - self.keep_archives)]:
            try:
                stale.unlink()
            except OSError:
                pass

    # -- writing ----------------------------------------------------------- #
    def record(self, event: str, **fields) -> dict | None:
        """Append one sanitised event. Returns the entry, or ``None`` when disabled."""
        if not self.enabled:
            return None
        with self._lock:
            self._seq += 1
            entry = {
                "v": AUDIT_VERSION,
                "ts": now_iso(),
                "source": self.source,
                "session": self.session,
                "pid": self.pid,
                "seq": self._seq,
                "event": str(event),
            }
            entry.update(self.sanitizer.fields(fields))
            line = json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":"))
            if not line.endswith("\n"):
                line += "\n"
            try:
                if self._handle is None:
                    self._open()
                assert self._handle is not None
                if self._handle.tell() >= self.max_bytes:
                    self._rotate()
                self._handle.write(line)
                self._handle.flush()
            except Exception as exc:  # noqa: BLE001 - logging must never be fatal
                self._handle = None
                self._fail(exc)
                # One retry from a clean handle, then give up on this event.
                try:
                    self._open()
                    assert self._handle is not None
                    self._handle.write(line)
                    self._handle.flush()
                except Exception as exc2:  # noqa: BLE001
                    self._handle = None
                    self._fail(exc2)
                    return None
            return entry

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                except OSError:
                    pass
                self._handle = None


def _next_archive_index(directory: Path, name: str) -> int:
    stem = Path(name).stem
    highest = 0
    try:
        for candidate in directory.glob("%s.*.jsonl" % stem):
            tail = candidate.name[len(stem) + 1: -len(".jsonl")]
            if tail.isdigit():
                highest = max(highest, int(tail))
    except OSError:
        return 0
    return highest


# --------------------------------------------------------------------------- #
# process-wide instance + request correlation
# --------------------------------------------------------------------------- #
_LOG: AuditLog | None = None

_CURRENT_REQUEST: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "blender_mcp_audit_request", default=None
)


def get_audit() -> AuditLog:
    """The process-wide logger (created lazily so import stays side-effect free)."""
    global _LOG
    if _LOG is None:
        _LOG = AuditLog()
    return _LOG


def configure(**kwargs) -> AuditLog:
    """Replace the process-wide logger (used by the server entry point and tests).

    An explicit ``directory`` also becomes the directory that readers
    (:func:`audit_dir`, the log tool and the dashboard) use, so a configured
    writer and its readers can never disagree.
    """
    global _LOG, _DIR_OVERRIDE
    directory = kwargs.get("directory")
    if directory is not None:
        _DIR_OVERRIDE = Path(directory)
    existing = _LOG
    _LOG = AuditLog(**kwargs)
    if existing is not None:
        existing.close()
    return _LOG


def current_request_id() -> str | None:
    scope = _CURRENT_REQUEST.get()
    return scope.get("request_id") if scope else None


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


class request_scope:
    """Context manager binding one audit request id to the current task."""

    def __init__(self, request_id: str, **fields):
        self.request_id = request_id
        self.fields = fields
        self._token = None

    def __enter__(self) -> str:
        self._token = _CURRENT_REQUEST.set({"request_id": self.request_id, **self.fields})
        return self.request_id

    def __exit__(self, *exc_info) -> bool:
        if self._token is not None:
            _CURRENT_REQUEST.reset(self._token)
        return False


# --------------------------------------------------------------------------- #
# result summarisation (shared by the MCP tool layer and the bridge wrapper)
# --------------------------------------------------------------------------- #
def _content_items(result):
    """Content blocks of a ``CallToolResult`` or of a FastMCP content list."""
    if isinstance(result, (list, tuple)):
        return list(result)
    content = getattr(result, "content", None)
    if content is not None:
        return list(content)
    return None


def result_mapping(result) -> dict | None:
    """The JSON object a tool returned, whether it came back as dict or as content.

    FastMCP converts ``dict`` results into a single ``TextContent`` JSON block, so
    the audit layer has to look inside the content list to see the payload.
    """
    if isinstance(result, dict):
        return result
    for item in _content_items(result) or []:
        if getattr(item, "type", None) != "text":
            continue
        try:
            parsed = json.loads(getattr(item, "text", "") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def summarize_result(result, sanitizer: Sanitizer | None = None) -> dict:
    """A bounded, secret-free description of an MCP tool result.

    Images are reported as a byte count (never their base64) and a JSON payload is
    kept with every value sanitised.
    """
    clean = sanitizer or get_audit().sanitizer
    if isinstance(result, dict):
        return clean.fields(result)
    items = _content_items(result)
    if items is None:
        return clean.value(result)
    kinds: list[str] = []
    texts: list[str] = []
    image_bytes = 0
    for item in items:
        kind = getattr(item, "type", None) or type(item).__name__
        kinds.append(str(kind))
        if kind == "text":
            texts.append(getattr(item, "text", "") or "")
        elif kind == "image":
            data = getattr(item, "data", "") or ""
            # Exact decoded size without materialising the image again.
            image_bytes += max(0, (len(data) // 4) * 3 - data.count("="))
    summary: dict = {"_type": "content", "content_types": kinds, "image_bytes": image_bytes or None}
    mapping = result_mapping(result)
    if mapping is not None:
        summary["result"] = clean.fields(mapping)
    elif texts:
        summary["text_preview"] = clean.scrub_text(texts[0])[:MAX_STRING]
    return summary


def record_tool_event(event: str, **fields) -> None:
    get_audit().record(event, **fields)


# --------------------------------------------------------------------------- #
# reading events back (used by the MCP log tool and the dashboard)
# --------------------------------------------------------------------------- #
def audit_files(directory: Path | str | None = None) -> list[Path]:
    """Every ``audit-*.jsonl`` file (live + archives), newest first."""
    root = Path(directory) if directory else audit_dir()
    try:
        files = [p for p in root.glob("%s*.jsonl" % FILE_PREFIX) if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return files[:MAX_TAIL_FILES]


def _read_tail(path: Path) -> list[dict]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > MAX_TAIL_BYTES_PER_FILE:
                handle.seek(size - MAX_TAIL_BYTES_PER_FILE)
                handle.readline()  # drop the truncated first line
            raw = handle.read()
    except OSError:
        return []
    events = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict):
            events.append(entry)
    return events


def tail_events(
    limit: int = 200,
    status: str | None = None,
    source: str | None = None,
    query: str | None = None,
    directory: Path | str | None = None,
) -> list[dict]:
    """Newest-last list of events matching the filters, bounded on every axis."""
    limit = max(1, min(int(limit or 200), MAX_EVENTS_READ))
    events: list[dict] = []
    for path in audit_files(directory):
        events.extend(_read_tail(path))
    events.sort(key=lambda e: (str(e.get("ts", "")), int(e.get("pid", 0) or 0), int(e.get("seq", 0) or 0)))
    if source:
        wanted = str(source).lower()
        events = [e for e in events if str(e.get("source", "")).lower() == wanted]
    if status:
        events = [e for e in events if _event_status(e) == status]
    if query:
        needle = str(query).lower()
        events = [e for e in events if needle in json.dumps(e, ensure_ascii=False, default=str).lower()]
    return events[-limit:]


def _event_status(entry: dict) -> str:
    """The single status word the dashboard shows for one *event*."""
    if entry.get("display_status"):
        return str(entry["display_status"])
    status = entry.get("status")
    if status:
        return str(status)
    event = str(entry.get("event", ""))
    if event.endswith("_start") or event in ("tool_start", "command_start"):
        return "running"
    if event in ("batch_rollback",):
        return "rolled_back"
    if event.endswith("_end") or event in ("tool_end", "command_end"):
        return "error" if entry.get("ok") is False else "ok"
    return "info"


def status_counts(events: list[dict]) -> dict:
    """Per-event status histogram (used by ``blender_operation_log``).

    This counts *events*, so a historical ``tool_start`` still shows up as
    ``running``. The dashboard uses :func:`annotate_operations` instead, which
    pairs starts with their ends and only counts genuinely outstanding calls.
    """
    counts: dict[str, int] = {}
    for entry in events:
        key = _event_status(entry)
        counts[key] = counts.get(key, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# operation accounting (pending vs completed)
# --------------------------------------------------------------------------- #
START_KINDS = ("tool_start", "command_start")
END_KINDS = ("tool_end", "command_end")
#: events that are one operation on their own (not a start/end pair)
SINGLE_KIND_STATUS = {"bridge_ok": "ok", "bridge_error": "error"}
#: events that carry information about an operation rather than being one
INFO_KINDS = (
    "batch_step", "batch_rollback", "session_start", "session_stop",
    "bridge_audit_unavailable", "bridge_audit_regained",
)


def _pair_key(entry: dict) -> tuple:
    """Identity of one operation: same writer + session + request + command/tool.

    ``request_id`` is present whenever the correlation field reached the add-on,
    but a directly dispatched command has none - then the command name is used so
    a start can still be matched with its own end inside the same session.
    """
    label = entry.get("tool") or entry.get("command") or entry.get("event")
    return (
        str(entry.get("source", "")),
        str(entry.get("session", "")),
        str(entry.get("pid", "")),
        str(entry.get("request_id") or "-"),
        str(label or "-"),
    )


def _status_of(entry: dict) -> str:
    status = entry.get("status")
    if status in ("ok", "error", "rolled_back", "running"):
        return str(status)
    if entry.get("ok") is False:
        return "error"
    return "ok"


def annotate_operations(events: list[dict]) -> tuple[list[dict], dict]:
    """Pair starts with ends and annotate every event with its display status.

    Returns ``(annotated_events, summary)`` where ``summary`` counts **operations**
    (not events): a start whose end was seen is one completed operation, a start
    without an end in a session that already stopped is historical ``info``, and
    only a start without an end in a live session counts as ``running``. That is
    what makes the dashboard's "进行中" number match reality after a session ends.
    """
    ended_sessions = {
        (str(entry.get("source", "")), str(entry.get("session", "")), str(entry.get("pid", "")))
        for entry in events
        if entry.get("event") == "session_stop"
    }
    ends: dict[tuple, list[int]] = {}
    for index, entry in enumerate(events):
        if entry.get("event") in END_KINDS:
            ends.setdefault(_pair_key(entry), []).append(index)

    annotated: list[dict] = []
    used_ends: set[int] = set()
    counts = {"ok": 0, "error": 0, "running": 0, "rolled_back": 0, "info": 0}
    for index, entry in enumerate(events):
        copy = dict(entry)
        kind = str(entry.get("event", ""))
        session_key = (str(entry.get("source", "")), str(entry.get("session", "")), str(entry.get("pid", "")))
        if kind in START_KINDS:
            match = next((i for i in ends.get(_pair_key(entry), []) if i > index and i not in used_ends), None)
            if match is not None:
                used_ends.add(match)
                status = _status_of(events[match])
                counts[status if status in counts else "info"] += 1
                copy["display_status"] = "completed" if status == "ok" else status
                copy["paired_with_seq"] = events[match].get("seq")
                copy["operation_status"] = status
            elif session_key in ended_sessions:
                counts["info"] += 1
                copy["display_status"] = "info"
                copy["operation_status"] = "orphaned"
            else:
                counts["running"] += 1
                copy["display_status"] = "running"
                copy["operation_status"] = "running"
        elif kind in END_KINDS:
            if index in used_ends:
                copy["display_status"] = "info"
                copy["operation_status"] = "paired"
                copy["paired"] = True
            else:
                used_ends.add(index)
                status = _status_of(entry)
                counts[status if status in counts else "info"] += 1
                copy["display_status"] = status
                copy["operation_status"] = status
        elif kind in SINGLE_KIND_STATUS:
            status = SINGLE_KIND_STATUS[kind]
            counts[status] += 1
            copy["display_status"] = status
            copy["operation_status"] = status
        elif kind in INFO_KINDS:
            # Detail/marker rows keep their own colour in the list but are NOT
            # operations (a rolled-back batch is already counted on its tool_end).
            counts["info"] += 1
            copy["display_status"] = str(entry.get("status") or "info")
            copy["operation_status"] = "info"
        else:
            counts["info"] += 1
            copy["display_status"] = "info"
            copy["operation_status"] = "info"
        annotated.append(copy)

    operations = counts["ok"] + counts["error"] + counts["running"] + counts["rolled_back"]
    summary = {
        **counts,
        "operations": operations,
        "events": len(events),
        "ended_sessions": len(ended_sessions),
        "note": (
            "counts are operations (an MCP call and its bridge command are two operations); "
            "'running' counts only starts whose session is still live, so a finished or ended "
            "session can never leave work looking pending"
        ),
    }
    return annotated, summary


def operation_summary(events: list[dict]) -> dict:
    _annotated, summary = annotate_operations(events)
    return summary


def describe_directory(directory: Path | str | None = None) -> dict:
    root = Path(directory) if directory else audit_dir()
    files = audit_files(root)
    return {
        "directory": str(root),
        "exists": root.exists(),
        "files": len(files),
        "newest": str(files[0]) if files else None,
        "bytes": sum(p.stat().st_size for p in files if p.exists()),
        "limits": {
            "max_string": MAX_STRING,
            "max_items": MAX_ITEMS,
            "max_depth": MAX_DEPTH,
            "max_tail_files": MAX_TAIL_FILES,
            "max_tail_bytes_per_file": MAX_TAIL_BYTES_PER_FILE,
        },
    }


# --------------------------------------------------------------------------- #
# transparent bridge instrumentation
# --------------------------------------------------------------------------- #
def _detect_addon_audit_support(original_call) -> bool:
    """Ask the running add-on whether it understands ``RESERVED_PARAM``.

    The probe is the non-mutating ``health`` command and is sent **without** the
    reserved field, so an older add-on is never sent a parameter it would reject.
    """
    try:
        health = original_call("health", {}, timeout=5.0)
    except Exception:  # noqa: BLE001 - a dead bridge reports its own error later
        return False
    if isinstance(health, dict):
        try:
            return int(health.get("audit_protocol", 0) or 0) >= ADDON_AUDIT_PROTOCOL
        except (TypeError, ValueError):
            return False
    return False


def _audit_protocol_of(result) -> int:
    if not isinstance(result, dict):
        return 0
    try:
        return int(result.get("audit_protocol", 0) or 0)
    except (TypeError, ValueError):
        return 0


#: Transport failures after which the bridge identity may have changed (Blender
#: restarted / the add-on was reloaded), so the capability probe is re-armed.
RECONNECT_CODES = ("bridge_unavailable", "bridge_timeout", "bridge_protocol_error")


def instrument_bridge(client, log: AuditLog | None = None):
    """Wrap ``client.call`` so every bridge command is audited and correlated.

    The wrapper adds the reserved ``_mcp_audit`` field to the parameters carrying
    this request's audit id, but only once the running add-on has announced that
    it strips that field. An older - or not yet started - Blender therefore keeps
    working unchanged, and correlation is *regained* automatically: the probe is
    re-armed after any transport failure and whenever a ``health`` answer reports
    ``audit_protocol`` again (the normal "call health first, launch Blender, call
    again" workflow). No request is ever replayed to achieve that.
    """
    explicit_log = log
    original_call = client.call
    state = {"capable": None, "probes": 0, "probes_failed": 0, "regained": 0}

    def audited_call(command, params=None, timeout=None, **kwargs):
        logger = explicit_log or get_audit()
        request_id = current_request_id() or new_request_id()
        data = dict(params or {})
        if state["capable"] is None:
            previous_failures = state["probes_failed"]
            state["capable"] = _detect_addon_audit_support(original_call)
            state["probes"] += 1
            # The probe must not leave a half-used connection behind: a probe that
            # failed (for example with a wrong token) can have closed the socket,
            # and reusing it would turn the real error into a protocol error.
            try:
                client.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
            if not state["capable"]:
                state["probes_failed"] += 1
                logger.record(
                    "bridge_audit_unavailable",
                    request_id=request_id,
                    correlation="unavailable",
                    probe_attempts=state["probes"],
                    note="the running Blender add-on does not announce audit support (or is not up yet); "
                         "bridge-side correlation is retried after a transport failure and on every "
                         "health answer that reports audit_protocol",
                )
            elif previous_failures:
                # The normal workflow - call health before Blender is up, launch it,
                # call again - must regain correlation without replaying anything.
                state["regained"] += 1
                logger.record(
                    "bridge_audit_regained",
                    request_id=request_id,
                    correlation="restored",
                    audit_protocol=ADDON_AUDIT_PROTOCOL,
                    failed_probes=previous_failures,
                    regained_count=state["regained"],
                    note="the add-on answered after %d failed probe(s); this request carries the "
                         "correlation id again" % previous_failures,
                )
        if state["capable"]:
            data[RESERVED_PARAM] = {"request_id": request_id, "session": logger.session}
        started = time.perf_counter()
        try:
            result = original_call(command, data, timeout=timeout, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged
            logger.record(
                "bridge_error",
                request_id=request_id,
                command=command,
                code=getattr(exc, "code", type(exc).__name__),
                message=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )
            if getattr(exc, "code", "") in RECONNECT_CODES:
                # The peer may be a different Blender now: re-arm the probe so the
                # next call can regain correlation. The failed request is NOT replayed.
                state["capable"] = None
            raise
        if not state["capable"] and command == "health":
            protocol = _audit_protocol_of(result)
            if protocol >= ADDON_AUDIT_PROTOCOL:
                state["capable"] = True
                state["regained"] += 1
                logger.record(
                    "bridge_audit_regained",
                    request_id=request_id,
                    correlation="restored",
                    audit_protocol=protocol,
                    regained_count=state["regained"],
                    note="the add-on now announces audit support; the next bridge call carries the "
                         "correlation id again",
                )
        logger.record(
            "bridge_ok",
            request_id=request_id,
            command=command,
            correlation="linked" if state["capable"] else "unavailable",
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            result=logger.sanitizer.value(result, key="result"),
        )
        return result

    client.call = audited_call  # type: ignore[method-assign]
    return client
