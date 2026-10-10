"""Graph safety cases independent of the existing geometry edit test file."""
from unittest.mock import patch
from tests.test_geometry_nodes_edit import Harness, Failure


class GraphSafetyTests(Harness):
    def insert(self, link=None):
        link = self.link_in if link is None else link
        return self.call("geometry_nodes_reroute_insert", group="Group", links=[{
            "from_node": link.from_node.name, "from_socket": link.from_socket.identifier,
            "to_node": link.to_node.name, "to_socket": link.to_socket.identifier}],
            points=[[-200.0, 50.0]])

    def test_muted_wire_survives_insert_and_delete(self):
        self.link_in.is_muted = True
        before = self.module.semantic_signature(self.module.capture_graph(self.tree, Failure))
        inserted = self.insert()
        self.assertTrue(inserted["validation"]["muted_links_equal"])
        name = inserted["created"][0]["node"]
        self.call("geometry_nodes_reroute_delete", group="Group", nodes=[name])
        self.assertEqual(before, self.module.semantic_signature(self.module.capture_graph(self.tree, Failure)))
        self.assertTrue(next(link for link in self.tree.links if link.from_node is self.input).is_muted)

    def test_upstream_mute_survives_anchor_removal(self):
        name = self.insert()["created"][0]["node"]
        incoming = next(link for link in self.tree.links if link.to_node.name == name)
        incoming.is_muted = True
        result = self.call("geometry_nodes_reroute_delete", group="Group", nodes=[name])
        self.assertTrue(result["validation"]["equivalent"])
        self.assertTrue(next(link for link in self.tree.links if link.from_node is self.input).is_muted)

    def test_failed_edit_preserves_unrelated_mute(self):
        self.link_out.is_muted = True
        before = self.module.capture_graph(self.tree, Failure)
        self.tree.links.fail_after_calls(2)
        with self.assertRaises(Failure) as caught:
            self.insert()
        self.assertTrue(caught.exception.details["rolled_back"])
        after = self.module.capture_graph(self.tree, Failure)
        self.assertEqual(before["nodes"], after["nodes"])
        self.assertEqual(self.module.semantic_signature(before), self.module.semantic_signature(after))

    def test_failed_order_restoration_does_not_claim_rollback(self):
        snapshot = self.module.capture_graph(self.tree, Failure)
        with patch.object(self.module, "_apply_link_order", return_value={"ok": False}):
            with self.assertRaises(Failure) as caught:
                self.module.restore_graph(self.tree, snapshot, Failure)
        self.assertEqual(caught.exception.code, "rollback_failed")
        self.assertFalse(caught.exception.details["rolled_back"])

    def test_recreated_anchor_retains_label_and_display_state(self):
        name = self.insert()["created"][0]["node"]
        reroute = self.tree.nodes.get(name)
        reroute.label = "Keep this label"
        reroute.mute = True
        reroute.hide = True
        reroute.use_custom_color = True
        reroute.color = [0.25, 0.5, 0.75]
        snapshot = self.module.capture_graph(self.tree, Failure)
        self.tree.nodes.remove(reroute)
        restored = self.module.restore_graph(self.tree, snapshot, Failure)
        self.assertTrue(restored["verified"])
        self.assertEqual(snapshot["nodes"], self.module.capture_graph(self.tree, Failure)["nodes"])
