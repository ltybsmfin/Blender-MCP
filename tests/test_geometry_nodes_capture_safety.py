"""Exact editor target and original nested/Shader Editor restoration."""
from types import SimpleNamespace as NS
from unittest.mock import patch
from tests.test_geometry_nodes_capture import AddonCase, FakeTree, FakePath, FakeArea, Failure


class EditorSafetyTests(AddonCase):
    def test_draw_before_smooth_navigation_finishes_is_not_capture_evidence(self):
        self.bpy.context.preferences = NS(view=NS(smooth_view=200))
        prepared = self.prepare(fit="ALL")
        space = self.editor_space()[1]
        space.paint()
        status = self.command("gn_editor_capture_status")(session=prepared["session"])
        self.assertFalse(status["match"]["checks"]["view_settled"])
        with patch.object(self.common.time, "time", return_value=prepared["expect"]["draw_after_epoch"] + 1):
            space.paint()
        status = self.command("gn_editor_capture_status")(session=prepared["session"])
        self.assertTrue(status["match"]["matches"])
        self.command("gn_editor_capture_abort")(session=prepared["session"])

    def test_default_uses_large_canvas_but_explicit_area_stays_exact(self):
        narrow = FakeArea("PROPERTIES", width=250, height=640)
        self.bpy.window.screen.areas.insert(0, narrow)
        prepared = self.prepare()
        self.assertEqual(prepared["area_index"], 1)
        self.command("gn_editor_capture_abort")(session=prepared["session"])
        prepared = self.prepare(area_index=0)
        self.assertEqual(prepared["area_index"], 0)
        self.command("gn_editor_capture_abort")(session=prepared["session"])

    def test_fit_waits_for_sidebar_canvas_and_node_dimensions(self):
        ready = []
        def redraw(**kwargs):
            ready.append(True)
            self.area.regions[0].width = 560
        def fit(**kwargs):
            self.assertTrue(ready, "fitting an undrawn editor crops node labels")
            self.assertEqual(self.bpy.context.region.width, 560)
        self.bpy.ops.wm.redraw_timer.on_call = redraw
        self.bpy.ops.node.view_all.on_call = fit
        prepared = self.prepare(fit="ALL")
        self.command("gn_editor_capture_abort")(session=prepared["session"])

    def test_cancelled_fit_is_error_and_restores_original_editor(self):
        self.bpy.ops.node.view_all.result = ("CANCELLED",)
        with self.assertRaises(Failure) as caught:
            self.prepare(fit="ALL")
        self.assertEqual(caught.exception.code, "fit_failed")
        self.assertEqual(self.area.type, "VIEW_3D")

    def original_editor(self, tree):
        self.area.type = "NODE_EDITOR"
        space = self.editor_space()[1]
        space.node_tree = tree
        return space

    def test_existing_target_is_pinned_during_capture(self):
        space = self.original_editor(self.tree)
        space.pin = False
        prepared = self.prepare()
        self.assertTrue(space.pin)
        result = self.command("gn_editor_capture_abort")(session=prepared["session"])
        self.assertTrue(result["restored"])
        self.assertFalse(space.pin)

    def test_nested_path_is_restored(self):
        root = FakeTree("OriginalRoot")
        child = FakeTree("OriginalChild")
        self.bpy.data.node_groups.extend([root, child])
        space = self.original_editor(root)
        space.path = FakePath([root, child])
        prepared = self.prepare()
        self.assertEqual(len(space.path), 0)
        result = self.command("gn_editor_capture_abort")(session=prepared["session"])
        self.assertTrue(result["restored"])
        self.assertIs(space.node_tree, root)
        self.assertEqual(space.path.trees, [root, child])

    def test_shader_tree_with_duplicate_name_is_restored_by_pointer(self):
        shader = FakeTree("NodeTree", bl_idname="ShaderNodeTree")
        duplicate = FakeTree("NodeTree", bl_idname="ShaderNodeTree")
        self.bpy.data.materials = [NS(node_tree=duplicate), NS(node_tree=shader)]
        space = self.original_editor(shader)
        space.tree_type = "ShaderNodeTree"
        space.pin = True
        space.path = FakePath([shader])
        prepared = self.prepare()
        result = self.command("gn_editor_capture_abort")(session=prepared["session"])
        self.assertTrue(result["restored"])
        self.assertIs(space.node_tree, shader)
        self.assertEqual(space.tree_type, "ShaderNodeTree")
        self.assertTrue(space.pin)
        self.assertEqual(space.path.trees, [shader])

    def test_required_metadata_failure_refuses_publication_and_restores_ui(self):
        prepared = self.prepare()
        _area, space = self.editor_space()
        space.paint()
        def broken():
            raise RuntimeError("layout no longer readable")
        self.common._SESSIONS[prepared["session"]]["metadata_provider"] = broken
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_capture_commit")(session=prepared["session"],
                                                      relative_path="previews/no_evidence.png")
        self.assertEqual(caught.exception.code, "capture_metadata_failed")
        self.assertFalse((self.root / "Project/previews/no_evidence.png").exists())
        self.assertEqual(self.area.type, "VIEW_3D")
        self.assertNotIn(prepared["session"], self.common._SESSIONS)
