"""Named opening of an existing ``.blend`` inside ``<workspace>/Project``.

Bridge-side command module.  The add-on registers it exactly like the other
optional command modules::

    import file_open
    file_open.register_commands(sys.modules[__name__])

It exposes one command, ``open_blend(path, confirm_replace_current=False,
load_ui=False)``, which is the *only* way this bridge opens another file.

Contract
--------
* ``path`` must be a **normalized absolute** ``.blend`` path inside the
  ``Project`` root.  Relative paths, UNC paths, ``\\\\?\\``/``\\\\.\\`` device
  paths, NUL characters, ``.``/``..``/duplicate/trailing separators, paths that
  escape ``Project``, symlink/junction/reparse components, missing files,
  directories, wrong extensions and unreadable files are refused *before*
  Blender touches the session.  The rules are applied through the existing
  ``resolve_project_path`` helper, so both layers of the server agree; a missing
  directory is never created in order to open something.
* The file must be provably readable *and* parseable by this Blender build
  **before** anything is replaced: the existing read-only inspection route
  (``_blend_container`` plus ``_temp_blend_metadata``, i.e. a temporary
  ``bpy.data.temp_data()`` main and ``bpy.data.libraries.load`` used for
  *enumeration only* - the ``data_to`` side is ignored, so no datablock is
  loaded into the live ``bpy.data``).  A ``.blend`` suffix alone is never
  accepted as proof.
* Replacing the loaded file is always explicit: ``confirm_replace_current``
  must be ``true``.  ``bpy.data.is_dirty`` is reported as a *hint* only - it
  cannot prove that every direct RNA edit reached the file - so an unconfirmed
  call fails with ``unsaved_changes`` when the flag is set and with
  ``confirmation_required`` when it is clear or unknown.
* The operator is the official ``bpy.ops.wm.open_mainfile`` executed with the
  explicit ``EXEC_DEFAULT`` context in *this* instance.  ``use_scripts`` is
  hard-wired to ``False`` and is never exposed as a parameter; the RNA is
  introspected first and a build that does not expose ``filepath``/``load_ui``/
  ``use_scripts`` is refused with ``unsupported_api`` instead of being guessed
  at.  ``load_ui`` defaults to ``False``, which keeps the current window layout
  while opening the file.
* Nothing is saved implicitly, the target file is never written, no second
  Blender is started, and no data rollback is promised: if the load fails or
  reports something other than ``FINISHED``, the answer says exactly what the
  live state looks like afterwards.

Only ``bpy`` and the standard library are imported here.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import bpy

#: Names listed per ID type in the bounded preflight sample.
SAMPLE_LIMIT = 10
#: Longest path this command accepts (Windows MAX_PATH headroom).
MAX_PATH_CHARS = 4096


def register_commands(api):
    """Register the ``open_blend`` command on the add-on ``api`` module."""

    error = api.CommandError

    # -- small validation helpers ------------------------------------------ #
    def text(value, label):
        if type(value) is not str:
            raise error(
                "invalid_argument",
                "%s must be a string, got %s" % (label, type(value).__name__),
            )
        return value

    def boolean(value, label):
        if type(value) is not bool:
            raise error(
                "invalid_argument",
                "%s must be a boolean (true/false), got %s" % (label, type(value).__name__),
            )
        return value

    def safe(getter, default=None):
        try:
            return getter()
        except Exception:  # noqa: BLE001 - a state read must never mask the real error
            return default

    def windows():
        found = []
        try:
            managers = list(getattr(bpy.data, "window_managers", None) or [])
        except Exception:  # noqa: BLE001
            return found
        for manager in managers:
            try:
                found.extend(list(getattr(manager, "windows", None) or []))
            except Exception:  # noqa: BLE001
                continue
        return found

    def same_path(left, right):
        """Compare two path values as the same file without touching the disk.

        Accepts strings and ``os.PathLike`` because the command compares Blender's
        reported string filepath against its own validated ``Path`` target.
        """
        try:
            left_text = os.fspath(left)
            right_text = os.fspath(right)
        except TypeError:
            return False
        if not isinstance(left_text, str) or not isinstance(right_text, str):
            return False
        if not left_text or not right_text:
            return False
        try:
            return os.path.normcase(os.path.realpath(left_text)) == os.path.normcase(os.path.realpath(right_text))
        except (OSError, ValueError):
            return False

    def filepath_changed(before, after):
        """True only when two *known* filepaths differ.

        ``''`` is Blender's "unsaved/untitled session"; two unnamed sessions have
        not changed file, and an unnamed session that becomes a named one (or the
        reverse) has.  ``not same_path('', '')`` would wrongly report a change.
        """
        left = before if isinstance(before, str) else None
        right = after if isinstance(after, str) else None
        if not left and not right:
            return False
        if not left or not right:
            return True
        return not same_path(left, right)

    def absolute_syntax_ok(value, windows=None):
        """True for an absolute path with a root and (on Windows) a drive.

        ``os.path.isabs('\\\\dir')`` is True on Windows although the path has no
        drive and would resolve against the *current* drive, and ``'C:foo'`` is
        drive-relative; both are rejected.  UNC/device prefixes are not accepted
        either (they are reported earlier with their own codes).  ``windows``
        defaults to the running platform and keeps the rule set unit-testable for
        both platforms.
        """
        if value.startswith("\\\\") or value.startswith("//"):
            return False
        if windows is None:
            if not Path(value).is_absolute():
                return False
            windows = os.name == "nt"
        if windows:
            drive, tail = os.path.splitdrive(value)
            return bool(drive) and tail[:1] in ("\\", "/")
        return value.startswith("/")

    def live_state():
        """Scalar/string snapshot of the session - no RNA reference is retained."""
        scene = safe(lambda: bpy.context.scene, None)
        filepath = safe(lambda: bpy.data.filepath, None)
        dirty = safe(lambda: bpy.data.is_dirty, None)
        return {
            "blend_filepath": filepath if isinstance(filepath, str) else None,
            "scene": safe(lambda: scene.name if scene is not None else None),
            "is_dirty": dirty if type(dirty) is bool else None,
            "is_dirty_known": type(dirty) is bool,
            "objects_total": safe(lambda: len(bpy.data.objects), None),
            "scenes": safe(lambda: sorted(entry.name for entry in bpy.data.scenes), None),
            "window_count": len(windows()),
            "background": safe(lambda: bool(bpy.app.background), None),
        }

    # -- path policy (same rule set as resolve_project_path) --------------- #
    def absolute_blend_path(value):
        raw = text(value, "path").strip()
        if not raw:
            raise error("invalid_path", "path must not be empty")
        if "\x00" in raw:
            raise error("invalid_path", "path must not contain a NUL character")
        if len(raw) > MAX_PATH_CHARS:
            raise error(
                "invalid_path",
                "path is longer than %d characters" % MAX_PATH_CHARS,
                details={"length": len(raw)},
            )
        if raw.startswith("\\\\") or raw.startswith("//"):
            kind = "device_path" if raw[2:4] in ("?\\", ".\\", "?/", "./") else "unc_path"
            raise error(
                kind,
                "UNC and device paths are rejected; %s must live inside <workspace>/Project" % raw,
                details={"path": raw},
            )
        sealed = raw.replace("/", os.sep)
        normalized = os.path.normpath(sealed)
        if normalized != sealed:
            raise error(
                "invalid_path",
                "path must be a normalized absolute path without '.', '..', duplicate or trailing "
                "separators, got %r (normalized: %r)" % (raw, normalized),
                details={"path": raw, "normalized": normalized},
            )
        if not absolute_syntax_ok(normalized):
            raise error(
                "invalid_path",
                "path must be an absolute path with a drive and root inside <workspace>/Project "
                "(drive-relative 'C:foo' and rooted-without-drive '\\dir' are rejected), got %r" % raw,
                details={"path": raw},
            )
        root = api.project_dir()
        candidate = normalized
        try:
            relative = os.path.relpath(candidate, str(root))
        except ValueError as exc:
            # Windows raises ValueError when the two paths are on different volumes.
            raise error(
                "path_outside_project",
                "path is on a different volume than <workspace>/Project",
                details={"path": raw, "resolved": candidate, "project_root": str(root),
                         "reason": "different_drive", "message": str(exc)},
            ) from exc
        if relative == ".." or relative.startswith(".." + os.sep) or os.path.isabs(relative):
            raise error(
                "path_outside_project",
                "path must stay inside <workspace>/Project",
                details={"path": raw, "resolved": candidate, "project_root": str(root)},
            )
        try:
            # Reuses the add-on's own safety route: '..' escapes, symlink/junction
            # components and paths that resolve outside Project are refused here.
            target = api.resolve_project_path(relative)
        except error:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw traceback shape
            raise error("invalid_path", "path could not be validated: %s" % exc, details={"path": raw}) from exc
        if not target.exists():
            if target.suffix.lower() != ".blend":
                raise error(
                    "invalid_path",
                    "only .blend files can be opened, got %r" % target.name,
                    details={"path": str(target)},
                )
            raise error(
                "file_not_found",
                "no file at %s" % target,
                details={"path": str(target), "project_root": str(root)},
            )
        try:
            is_file = target.is_file()
        except OSError as exc:
            raise error("unreadable", "cannot stat %s: %s" % (target, exc), details={"path": str(target)}) from exc
        if not is_file:
            raise error(
                "not_a_file",
                "%s is not a regular file" % target,
                details={"path": str(target)},
            )
        if target.suffix.lower() != ".blend":
            raise error(
                "invalid_path",
                "only .blend files can be opened, got %r" % target.name,
                details={"path": str(target)},
            )
        try:
            with open(str(target), "rb") as handle:
                handle.read(1)
        except OSError as exc:
            raise error(
                "unreadable",
                "cannot read %s: %s" % (target, exc),
                details={"path": str(target), "errno": getattr(exc, "errno", None)},
            ) from exc
        return target

    # -- read-only preflight ---------------------------------------------- #
    def preflight(target):
        container_of = getattr(api, "_blend_container", None)
        metadata_of = getattr(api, "_temp_blend_metadata", None)
        if not callable(container_of) or not callable(metadata_of):
            raise error(
                "unsupported_api",
                "this add-on does not expose the read-only .blend inspection helpers "
                "(_blend_container / _temp_blend_metadata), so the file cannot be proven readable "
                "and parseable before the session file is replaced; refusing to open it",
            )
        try:
            container = container_of(target)
        except error:
            raise
        except OSError as exc:
            raise error(
                "unreadable",
                "cannot read %s: %s" % (target.name, exc),
                details={"path": str(target)},
            ) from exc
        if not isinstance(container, dict) or container.get("container") is None:
            magic = container.get("magic") if isinstance(container, dict) else None
            raise error(
                "invalid_blend",
                "%s has no recognizable .blend container (magic %s); the file was not opened and "
                "nothing was replaced" % (target.name, magic),
                details={"path": str(target), "container": container},
            )
        inspection_mode = "direct"
        scratch = None
        try:
            inspection_target = target
            if same_path(str(target), safe(lambda: bpy.data.filepath, "")):
                # Blender refuses libraries.load of the current Main filename,
                # even inside temp_data(). Enumerate an exact disk copy instead;
                # never save the modified live Main to obtain this preflight.
                runtime = Path(__file__).resolve().parents[1] / ".runtime"
                runtime.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix="open-preflight-", suffix=".blend", dir=str(runtime))
                os.close(fd)
                scratch = Path(name)
                before_stat = target.stat()
                shutil.copyfile(target, scratch)
                after_stat = target.stat()
                if (before_stat.st_size, before_stat.st_mtime_ns, before_stat.st_ino) != \
                   (after_stat.st_size, after_stat.st_mtime_ns, after_stat.st_ino):
                    raise error("source_changed", "the target file changed during read-only preflight; refusing to open it")
                inspection_target = scratch
                inspection_mode = "temporary_disk_copy"
            inventories = metadata_of(inspection_target)
        except error as exc:
            if getattr(exc, "code", "") == "source_changed":
                raise
            if getattr(exc, "code", "") == "api_unsupported":
                raise error(
                    "unsupported_api",
                    "the read-only ID inventory of %s cannot be read in this Blender build: %s"
                    % (target.name, exc.message),
                    details={"path": str(target)},
                ) from exc
            raise error(
                "blend_parse_failed",
                "this Blender build cannot read the ID inventories of %s, so the file is not proven "
                "parseable and was not opened: %s" % (target.name, exc.message),
                details={"path": str(target), "code": getattr(exc, "code", None), "details": exc.details},
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise error(
                "blend_parse_failed",
                "reading the ID inventories of %s failed: %s" % (target.name, exc),
                details={"path": str(target)},
            ) from exc
        finally:
            if scratch is not None:
                scratch.unlink(missing_ok=True)
        if not isinstance(inventories, dict):
            raise error(
                "blend_parse_failed",
                "the read-only ID inventory of %s is not an object" % target.name,
                details={"path": str(target)},
            )
        counts = {}
        sample = {}
        for attribute, names in inventories.items():
            listed = list(names) if isinstance(names, (list, tuple, set, frozenset)) else []
            counts[str(attribute)] = len(listed)
            if listed:
                sample[str(attribute)] = [str(name) for name in listed[:SAMPLE_LIMIT]]
        return {"container": container, "counts": counts, "sample": sample,
                "inspection_mode": inspection_mode}

    # -- official operator ------------------------------------------------ #
    def open_mainfile_operator():
        ops = getattr(bpy, "ops", None)
        operator = getattr(getattr(ops, "wm", None), "open_mainfile", None)
        if operator is None or not callable(operator):
            raise error(
                "unsupported_api",
                "bpy.ops.wm.open_mainfile is unavailable in this Blender build; refusing to open a "
                "file with a guessed API",
            )
        try:
            rna = operator.get_rna_type()
            properties = {prop.identifier: prop.type for prop in rna.properties}
        except Exception as exc:  # noqa: BLE001
            raise error(
                "unsupported_api",
                "bpy.ops.wm.open_mainfile cannot be introspected (%s); refusing to guess its parameters" % exc,
            ) from exc
        required = {"filepath": "STRING", "load_ui": "BOOLEAN", "use_scripts": "BOOLEAN"}
        missing = sorted(name for name in required if name not in properties)
        wrong = sorted(
            "%s=%s" % (name, properties[name])
            for name, kind in required.items()
            if name in properties and properties[name] != kind
        )
        if missing or wrong:
            raise error(
                "unsupported_api",
                "bpy.ops.wm.open_mainfile does not expose the exact properties this command requires, "
                "so it cannot guarantee 'no scripts' and 'keep UI': missing %s, wrong type %s" % (missing, wrong),
                details={"required": required, "available": sorted(properties)},
            )
        return operator

    @api.command(
        "open_blend",
        summary="open an existing .blend inside Project as the session file (always requires confirmation)",
    )
    def open_blend(path, confirm_replace_current=False, load_ui=False):
        boolean(confirm_replace_current, "confirm_replace_current")
        boolean(load_ui, "load_ui")
        target = absolute_blend_path(path)

        # 1) Explicit confirmation is mandatory: the dirty flag is only a hint.
        if confirm_replace_current is not True:
            state = live_state()
            details = {
                "requested_path": str(target),
                "current_filepath": state["blend_filepath"],
                "is_dirty": state["is_dirty"],
                "is_dirty_known": state["is_dirty_known"],
                "would_replace_current_file": True,
                "current_scene": state["scene"],
                "note": "opening replaces the whole loaded session; pass confirm_replace_current=true "
                        "after saving or discarding your work. bpy.data.is_dirty is a hint only and "
                        "cannot prove that direct RNA edits are on disk.",
            }
            if state["is_dirty"]:
                raise error(
                    "unsaved_changes",
                    "the current session has unsaved changes (%s); nothing was opened. Save or discard "
                    "them, then call again with confirm_replace_current=true" % (state["blend_filepath"] or "<untitled>"),
                    details=details,
                )
            raise error(
                "confirmation_required",
                "open_blend always replaces the loaded file and needs confirm_replace_current=true; "
                "nothing was opened (current file: %s)" % (state["blend_filepath"] or "<untitled>"),
                details=details,
            )

        # 2) Read-only proof that this file is readable and parseable.  The live
        #    state is compared before/after including the dirty flag, and the
        #    add-on's own full fingerprint is used when this build exposes it, so
        #    "live_state_unchanged" never hides an observable change.
        before = live_state()
        fingerprint_of = getattr(api, "_live_state_fingerprint", None)
        before_fingerprint = safe(fingerprint_of) if callable(fingerprint_of) else None
        preflight_report = preflight(target)
        after_fingerprint = safe(fingerprint_of) if callable(fingerprint_of) else None
        after_preflight = live_state()
        observed = {
            key: {"before": before[key], "after": after_preflight[key]}
            for key in before
            if before[key] != after_preflight[key]
        }
        detailed = {}
        if isinstance(before_fingerprint, dict) and isinstance(after_fingerprint, dict):
            detailed = {
                key: {"before": before_fingerprint[key], "after": after_fingerprint.get(key)}
                for key in before_fingerprint
                if before_fingerprint[key] != after_fingerprint.get(key)
            }
            proved_by = "api._live_state_fingerprint"
            compared = sorted(before_fingerprint)
        else:
            proved_by = "local scalar snapshot (filepath/scene/scenes/objects/windows/dirty)"
            compared = sorted(before)
        drift = {**observed, **detailed}
        if drift:
            raise error(
                "preflight_changed_state",
                "the read-only preflight of %s changed live Blender state (%s); refusing to open"
                % (target.name, ", ".join(sorted(drift))),
                details={"delta": drift, "proved_by": proved_by, "compared": compared},
            )

        # 3) Official operator with official parameters only.
        operator = open_mainfile_operator()

        # 4) Load.
        try:
            outcome = operator("EXEC_DEFAULT", filepath=str(target), load_ui=bool(load_ui), use_scripts=False)
        except error:
            raise
        except Exception as exc:  # noqa: BLE001 - report the real state, promise no rollback
            state = live_state()
            raise error(
                "load_failed",
                "bpy.ops.wm.open_mainfile failed for %s: %s" % (target, exc),
                details={
                    "stage": "exec",
                    "requested_path": str(target),
                    "current_filepath": state["blend_filepath"],
                    "current_filepath_changed": filepath_changed(state["blend_filepath"],
                                                                 before["blend_filepath"]),
                    "state": state,
                    "rollback": "not_attempted",
                    "note": "the previous file was neither saved nor restored; the answer describes the "
                            "live state as it is now",
                },
            ) from exc

        if isinstance(outcome, (set, frozenset, list, tuple)):
            report = sorted(str(item) for item in outcome)
        else:
            report = [str(outcome)]
        if report != ["FINISHED"]:
            state = live_state()
            raise error(
                "load_failed",
                "bpy.ops.wm.open_mainfile returned %s instead of {'FINISHED'} for %s"
                % (report, target),
                details={
                    "stage": "operator_result",
                    "operator_result": report,
                    "requested_path": str(target),
                    "current_filepath": state["blend_filepath"],
                    "current_filepath_changed": filepath_changed(state["blend_filepath"],
                                                                 before["blend_filepath"]),
                    "state": state,
                    "rollback": "not_attempted",
                },
            )

        # 5) Re-read the normalized path and touch the current scene.
        after = live_state()
        if not same_path(after["blend_filepath"], target):
            raise error(
                "load_failed",
                "the operator reported FINISHED but the loaded file is %r instead of the requested %r"
                % (after["blend_filepath"], str(target)),
                details={
                    "stage": "verify_filepath",
                    "requested_path": str(target),
                    "expected": os.path.normcase(os.path.realpath(str(target))),
                    "actual": after["blend_filepath"],
                    "current_filepath_changed": filepath_changed(after["blend_filepath"],
                                                                 before["blend_filepath"]),
                    "state": after,
                    "rollback": "not_attempted",
                },
            )
        if after["scene"] is None:
            raise error(
                "load_failed",
                "%s was loaded but no current scene could be read; the session may be unusable"
                % target.name,
                details={"stage": "verify_scene", "requested_path": str(target), "state": after,
                         "rollback": "not_attempted"},
            )

        return {
            "opened": True,
            "path": str(target),
            "previous": {
                "blend_filepath": before["blend_filepath"],
                "scene": before["scene"],
                "is_dirty": before["is_dirty"],
                "is_dirty_known": before["is_dirty_known"],
            },
            "current": after,
            "operator": "wm.open_mainfile",
            "exec_context": "EXEC_DEFAULT",
            "load_ui": bool(load_ui),
            "use_scripts": False,
            "confirmed_replace_current": True,
            "saved_before_open": False,
            "target_written": False,
            "pid": os.getpid(),
            "blender_version": safe(lambda: bpy.app.version_string),
            "ui_mode": {
                "background": after["background"],
                "load_ui": bool(load_ui),
                "window_count": after["window_count"],
            },
            "preflight": {
                "read_only": True,
                "container": preflight_report["container"],
                "counts": preflight_report["counts"],
                "sample": preflight_report["sample"],
                "inspection_mode": preflight_report["inspection_mode"],
                "live_state_unchanged": True,
                "proved_by": proved_by,
                "compared": compared,
            },
            "rollback": "not_attempted",
        }
