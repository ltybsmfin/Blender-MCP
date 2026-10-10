"""Verify supported bootstrap refresh preserves an existing bridge/session."""
import ast
import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class RefreshTests(unittest.TestCase):
    def exercise(self, resident=False, mismatch=False):
        source = Path(__file__).resolve().parents[1] / "blender" / "bootstrap.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        addon = types.ModuleType("blender_mcp_addon")
        addon._Logger = Mock(return_value=Mock())
        addon._log_path = Mock(return_value="fixture.log")
        addon.register = Mock(side_effect=ValueError("already registered as a subclass"))
        addon.read_or_create_token = Mock(return_value="fixture-only-secret")
        addon.probe_existing_bridge = Mock(return_value={"blender_version": "5.2.1 LTS", "pid": 42})
        addon.start_bridge, addon.stop_bridge = Mock(), Mock()
        addon.normalize_loopback_host = lambda value: value
        if resident:
            addon._SERVER = types.SimpleNamespace(running=True, host="127.0.0.1", port=9876,
                _token="different-fixture-secret" if mismatch else "fixture-only-secret", address="127.0.0.1:9876")
            addon.probe_existing_bridge.side_effect = AssertionError("the main-thread listener cannot answer its own blocking probe")
        opened = types.ModuleType("file_open")
        opened.register_commands = Mock()
        outlined = types.ModuleType("outliner")
        outlined.register_commands = Mock()
        managed = types.ModuleType("outliner_management")
        managed.register_commands = Mock()
        review_modules = {}
        for name in ('review_capture_common', 'properties', 'geometry_nodes_edit',
                     'model_inspection', 'geometry_nodes_capture', 'review_maintenance'):
            module = types.ModuleType(name)
            module.register_commands = Mock()
            module._SESSIONS = {}
            review_modules[name] = module
        bpy = types.ModuleType("bpy")
        bpy.context = types.SimpleNamespace(scene="unchanged")
        options = dict(host="127.0.0.1", port=9876, token_file=None, project_dir=None,
                       quiet=True, force=False, pump=False, no_timer=False, pump_seconds=0.0)
        namespace = {"parse_args": lambda _: options, "ADDON_DIR": source.parent,
                     "Path": Path, "sys": sys}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[main], type_ignores=[])), str(source), "exec"), namespace)
        with patch.dict(sys.modules, {"bpy": bpy, "blender_mcp_addon": addon, "file_open": opened, "outliner": outlined, "outliner_management": managed, **review_modules}), \
             patch("importlib.reload", side_effect=lambda module: module) as reload, \
             patch.dict("os.environ", {}, clear=False), contextlib.redirect_stdout(io.StringIO()):
            if mismatch:
                with self.assertRaisesRegex(RuntimeError, "different settings"):
                    namespace["main"]([])
                status = None
            else:
                status = namespace["main"]([])
        if mismatch:
            addon.register.assert_not_called()
            addon.probe_existing_bridge.assert_not_called()
            addon.start_bridge.assert_not_called()
            addon.stop_bridge.assert_not_called()
            return
        self.assertEqual(status, 0)
        self.assertEqual(reload.call_count, 9)
        self.assertEqual([call.args[0] for call in reload.call_args_list],
                         [opened, outlined, managed, *review_modules.values()])
        for name, module in review_modules.items():
            if name != 'review_capture_common':
                module.register_commands.assert_called_once_with(addon)
        opened.register_commands.assert_called_once_with(addon)
        outlined.register_commands.assert_called_once_with(addon)
        managed.register_commands.assert_called_once_with(addon, outlined.register_commands.return_value)
        addon.register.assert_not_called()
        addon.start_bridge.assert_not_called()
        addon.stop_bridge.assert_not_called()
        self.assertEqual(bpy.context.scene, "unchanged")
        if resident:
            addon.probe_existing_bridge.assert_not_called()

    def test_bootstrap_refreshes_isolated_commands_before_reusing_external_listener(self):
        self.exercise()

    def test_live_in_process_server_is_reused_without_blocking_self_probe(self):
        self.exercise(resident=True)

    def test_live_in_process_configuration_mismatch_never_stops_or_starts(self):
        self.exercise(resident=True, mismatch=True)


if __name__ == "__main__":
    unittest.main()
