"""Configuration, token handling and path safety for the Blender MCP server.

Everything here is plain standard library: the MCP server process must stay
importable without Blender, and the Blender side re-implements the same rules
with ``bpy`` available (see ``MCP/blender/blender_mcp_addon.py``).
"""

from __future__ import annotations

import os
import ipaddress
import secrets
import stat
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# locations
# --------------------------------------------------------------------------- #
# <workspace>/MCP/blender_mcp/config.py  ->  parents: [blender_mcp, MCP, workspace]
PACKAGE_DIR = Path(__file__).resolve().parent
MCP_DIR = PACKAGE_DIR.parent
WORKSPACE_ROOT = MCP_DIR.parent
RUNTIME_DIR = MCP_DIR / ".runtime"
TOKEN_PATH = RUNTIME_DIR / "token"
LOG_DIR = RUNTIME_DIR / "logs"
REPORT_DIR = MCP_DIR / "tests" / "reports"
PROJECT_DIR = WORKSPACE_ROOT / "Project"
BLENDER_DIR = MCP_DIR / "blender"
BOOTSTRAP_PATH = BLENDER_DIR / "bootstrap.py"
VERSION_DIR = WORKSPACE_ROOT / "Version"

# --------------------------------------------------------------------------- #
# bridge settings (overridable through the environment)
# --------------------------------------------------------------------------- #
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
DEFAULT_CALL_TIMEOUT = 30.0
CONNECT_TIMEOUT = 5.0
MAX_FRAME_BYTES = 8 * 1024 * 1024
TOKEN_BYTES = 32


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def bridge_host() -> str:
    host = os.environ.get("BLENDER_MCP_HOST", DEFAULT_HOST).strip()
    if host.lower() == "localhost":
        return DEFAULT_HOST
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("BLENDER_MCP_HOST must be an IPv4 loopback address") from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError("BLENDER_MCP_HOST must be an IPv4 loopback address")
    return str(address)


def bridge_port() -> int:
    return _env_int("BLENDER_MCP_PORT", DEFAULT_PORT)


def call_timeout() -> float:
    return _env_float("BLENDER_MCP_TIMEOUT", DEFAULT_CALL_TIMEOUT)


def project_dir() -> Path:
    override = os.environ.get("BLENDER_MCP_PROJECT_DIR")
    base = PROJECT_DIR.absolute()
    if _is_reparse_point(base):
        raise PathValidationError("Project root must not be a symlink or junction")
    requested = Path(override).absolute() if override else base
    if not _within(str(requested), str(base)):
        raise PathValidationError("BLENDER_MCP_PROJECT_DIR must stay inside the sibling Project directory")
    walk = base
    for part in requested.relative_to(base).parts:
        walk = walk / part
        if _is_reparse_point(walk):
            raise PathValidationError("project directory contains a symlink or junction")
    resolved = requested.resolve()
    if not _within(str(resolved), str(base.resolve())):
        raise PathValidationError("project directory resolves outside Project")
    return resolved


def token_path() -> Path:
    override = os.environ.get("BLENDER_MCP_TOKEN_FILE")
    return Path(override) if override else TOKEN_PATH


def bridge_address() -> str:
    return "%s:%d" % (bridge_host(), bridge_port())


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #
def ensure_token(path: Path | None = None) -> str:
    """Return the shared token, creating it when missing. Never logged."""
    target = Path(path) if path else token_path()
    try:
        if target.exists():
            token = target.read_text(encoding="utf-8").strip()
            if token:
                return token
        target.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(TOKEN_BYTES)
        target.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return token
    except OSError:
        # A read-only or sandboxed profile must not crash the server at import
        # time; the bridge call will report the real problem.
        return ""


def read_token(path: Path | None = None) -> str | None:
    target = Path(path) if path else token_path()
    try:
        token = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


# --------------------------------------------------------------------------- #
# path safety (mirrors the Blender-side rule set)
# --------------------------------------------------------------------------- #
class PathValidationError(ValueError):
    """Raised when a caller-supplied path leaves the project root."""


def _is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(str(path))
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _within(child: str, parent: str) -> bool:
    child_n = os.path.normcase(os.path.normpath(child))
    parent_n = os.path.normcase(os.path.normpath(parent))
    if child_n == parent_n:
        return True
    return child_n.startswith(parent_n.rstrip("\\/") + os.sep)


def resolve_project_path(relative_path: str) -> Path:
    """Resolve a path that must stay inside ``<workspace>/Project``."""
    if not relative_path or not str(relative_path).strip():
        raise PathValidationError("path must not be empty")
    raw = str(relative_path).strip().replace("/", os.sep)
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        raise PathValidationError("path must be relative to <workspace>/Project, got an absolute path")
    parts = [part for part in raw.split(os.sep) if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PathValidationError("path traversal ('..') is rejected")
    if not parts:
        raise PathValidationError("path does not name a file")

    root = project_dir().resolve()
    candidate = root.joinpath(*parts)
    walk = root
    for part in parts[:-1]:
        walk = walk / part
        if walk.exists() and _is_reparse_point(walk):
            raise PathValidationError("symlinked/junction directories are rejected: %s" % walk)
    if candidate.exists() and _is_reparse_point(candidate):
        raise PathValidationError("symlinked target files are rejected")
    real = Path(os.path.realpath(str(candidate)))
    if not _within(str(real), str(root)):
        raise PathValidationError("resolved path escapes <workspace>/Project: %s" % real)
    return real


# --------------------------------------------------------------------------- #
# Blender executable discovery
# --------------------------------------------------------------------------- #
def blender_executable_candidates() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("BLENDER_MCP_BLENDER")
    if env:
        candidates.append(Path(env))
    if VERSION_DIR.is_dir():
        for child in sorted(VERSION_DIR.iterdir(), reverse=True):
            if child.is_dir():
                for name in ("blender.exe", "blender"):
                    candidate = child / name
                    if candidate.is_file():
                        candidates.append(candidate)
    return candidates


def resolve_blender_executable(explicit: str | None = None) -> Path:
    """Explicit path > BLENDER_MCP_BLENDER > <workspace>/Version/*/blender.exe > PATH."""
    selected = explicit or os.environ.get("BLENDER_MCP_BLENDER")
    if selected:
        path = Path(selected)
        if path.is_file():
            return path.resolve()
        raise FileNotFoundError("blender executable not found at %s" % selected)
    for candidate in blender_executable_candidates():
        if candidate.is_file():
            return candidate.resolve()
    from shutil import which  # noqa: PLC0415 - only needed on this fallback

    found = which("blender")
    if found:
        return Path(found).resolve()
    raise FileNotFoundError(
        "no Blender executable found; set BLENDER_MCP_BLENDER or pass blender_path"
    )


def python_executable() -> str:
    """The interpreter that should run the MCP stdio server (this venv)."""
    return sys.executable


def describe_environment() -> dict:
    return {
        "server_python": sys.executable,
        "server_python_version": sys.version.split()[0],
        "workspace_root": str(WORKSPACE_ROOT),
        "mcp_dir": str(MCP_DIR),
        "project_dir": str(project_dir()),
        "token_path": str(token_path()),
        "token_present": token_path().exists(),
        "blender_bootstrap": str(BOOTSTRAP_PATH),
        "bridge_address": bridge_address(),
        "call_timeout_s": call_timeout(),
    }
