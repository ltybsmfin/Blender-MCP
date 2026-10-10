"""Optional extension adapter, executed only inside Blender."""
from __future__ import annotations

from pathlib import Path
import sys
import bpy

EXTENSION_ID = "blender_mcp_node_notes"
REPOSITORY_ID = "mcp_node_notes_local"
MODULE = "bl_ext." + REPOSITORY_ID + "." + EXTENSION_ID


def register_commands(api):
    def root():
        return api.workspace_root() / "Mod" / EXTENSION_ID / "repository"

    def status():
        version = list(bpy.app.version)
        base = {"blender_version": bpy.app.version_string, "blender_version_tuple": version,
                "module": MODULE, "repository": str(root()), "api_version": 1,
                "hint": "Enable the local extension from Mod using blender_node_note_extension; native Frame tools remain available."}
        if tuple(version) < (5, 2, 0) or not hasattr(bpy.types.NodeFrame, "bl_rna") or "text" not in bpy.types.NodeFrame.bl_rna.properties:
            return {**base, "available": False, "code": "unsupported_version"}, None
        module = sys.modules.get(MODULE)
        expected = (root() / EXTENSION_ID / "__init__.py").resolve()
        loaded_path = Path(getattr(module, "__file__", "")).resolve() if module else None
        if not expected.is_file():
            return {**base, "available": False, "code": "dependency_missing", "loaded_path": str(loaded_path) if loaded_path else None}, None
        if not module or not getattr(module, "REGISTERED", False) or MODULE not in bpy.context.preferences.addons:
            return {**base, "available": False, "code": "dependency_missing", "loaded_path": str(loaded_path) if loaded_path else None}, None
        if loaded_path != expected or getattr(module, "API_VERSION", None) != 1:
            return {**base, "available": False, "code": "unsupported_version", "loaded_path": str(loaded_path)}, None
        names = ("BLENDER_NODE_NOTE_OT_create", "BLENDER_NODE_NOTE_OT_update", "BLENDER_NODE_NOTE_OT_delete")
        # Module registration is confirmed by RNA operators, not just import.
        operators = {}
        for name in ("create", "update", "delete", "open_text"):
            try:
                operators[name] = getattr(bpy.ops.blender_node_note, name).get_rna_type().identifier
            except Exception:
                operators[name] = None
        if not all(operators.values()):
            return {**base, "available": False, "code": "dependency_missing", "operators": operators}, None
        return {**base, "available": True, "code": "ok", "loaded_path": str(loaded_path), "operators": operators}, module

    @api.command("node_note_status", mutating=False, summary="probe optional Mod extension in Blender, including RNA operators")
    def get_status():
        return status()[0]

    def invoke(method, params):
        detected, module = status()
        if not detected["available"]:
            raise api.CommandError(detected["code"], "optional node notes extension is unavailable", details=detected)
        try:
            return getattr(module, method)(**params)
        except Exception as exc:
            if isinstance(exc, getattr(module, "NoteError", ())):
                raise api.CommandError(exc.code, str(exc), details=getattr(exc, "details", {})) from exc
            raise api.CommandError("note_write_failed", str(exc), details={"method": method, "blender_version": bpy.app.version_string}) from exc

    @api.command("node_note_create", summary="create managed annotation with its own native Text")
    def create(group, frame, title="", body="", location=(0, 0)):
        return invoke("create_note", dict(group=group, frame=frame, title=title, body=body, location=location))

    @api.command("node_note_get", mutating=False, summary="read a managed native Frame annotation")
    def get(group, frame):
        return invoke("get_note", dict(group=group, frame=frame))

    @api.command("node_note_update", summary="noninteractive update; shared Text and ordinary Frames protected")
    def update(group, frame, body, title=None, allow_empty=False):
        return invoke("update_note", dict(group=group, frame=frame, body=body, title=title, allow_empty=allow_empty))

    @api.command("node_note_delete", summary="explicit confirmation; preserve Text unless removal was requested")
    def delete(group, frame, confirm=False, remove_text=False):
        return invoke("delete_note", dict(group=group, frame=frame, confirm=confirm, remove_text=remove_text))

    @api.command("node_note_extension", summary="enable/disable only the known extension from the fixed Mod local repository")
    def extension(enable=True, persist=False):
        import addon_utils
        if type(enable) is not bool or type(persist) is not bool:
            raise api.CommandError("invalid_value", "enable/persist must be boolean")
        if tuple(bpy.app.version) < (5, 2, 0):
            raise api.CommandError("unsupported_version", "requires Blender 5.2+", details=get_status())
        repository = root().resolve()
        mod_root = (api.workspace_root() / "Mod").resolve()
        if not repository.is_relative_to(mod_root) or not (repository / EXTENSION_ID / "blender_manifest.toml").is_file():
            raise api.CommandError("dependency_missing", "expected extension manifest is absent from Mod", details=get_status())
        if persist:
            config = Path(bpy.utils.user_resource("CONFIG")).resolve()
            if not any(config.is_relative_to(p.resolve()) for p in (api.workspace_root() / "MCP", mod_root)):
                raise api.CommandError("unauthorized_config", "persistent enable is restricted to a project-owned Blender profile", details={"config": str(config)})
        repos = bpy.context.preferences.extensions.repos
        matching = [r for r in repos if r.module == REPOSITORY_ID]
        if matching and Path(matching[0].directory).resolve() != repository:
            raise api.CommandError("repository_conflict", "repository identifier already points outside the expected Mod directory")
        created = None
        try:
            if enable:
                if not matching:
                    created = repos.new(name="MCP Node Notes (Mod)", module=REPOSITORY_ID, custom_directory=str(repository))
                    created.use_remote_url = False
                addon_utils.modules_refresh()
                failures = []
                module = addon_utils.enable(MODULE, default_set=True, handle_error=lambda *args: failures.append(True))
                if failures or module is None or not get_status()["available"]:
                    raise RuntimeError("extension did not register; check its manifest/API compatibility")
            else:
                addon_utils.disable(MODULE, default_set=True)
            if persist:
                result = bpy.ops.wm.save_userpref()
                if "FINISHED" not in result:
                    raise RuntimeError("preferences save did not finish")
        except Exception as exc:
            if created is not None:
                addon_utils.disable(MODULE, default_set=True)
                repos.remove(created)
            raise api.CommandError("extension_load_failed", str(exc), details={"repository": str(repository), "blender_version": bpy.app.version_string}) from exc
        return {**get_status(), "enabled": enable, "persisted": persist, "source_copied": False}
