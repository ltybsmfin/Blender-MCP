"""Input mode safety: a name is not the Blender 5.2 type selector."""
import types
from tests.test_properties import Fixture, DriverCurve, AnimationData


class ModeSafetyTests(Fixture):
    def test_target_view_layer_uses_rna_pointer_identity(self):
        self.obj.as_pointer = lambda: 1234
        self.scene.view_layers[0].objects.get = lambda name: types.SimpleNamespace(as_pointer=lambda: 1234)
        scene, layer = self.helpers['view_layer_for'](self.obj)
        self.assertIs(scene, self.scene)
        self.assertIs(layer, self.scene.view_layers[0])

    def test_live_array_readback_and_snapshot_do_not_alias_storage(self):
        class RNAArray:
            def __init__(self, values):
                self.values = values
            def to_list(self):
                return list(self.values)
        instance = self.instance_of()["Socket_2"]
        instance["value"] = RNAArray([0., 0., 0.])
        self.call("set_parameter", object="P", modifier="GN", identifier="Socket_2", value=[1., 2., 3.])
        from tests.test_properties import MODULE as properties
        source = RNAArray([0., 1., 2.])
        snapshot = properties._copy_value(source)
        source.values[0] = 9.
        self.assertEqual(snapshot, [0., 1., 2.])
        self.assertTrue(properties._same_value([0., 1., 2.], RNAArray([0., 1., 2.])))

    def test_nonempty_name_does_not_select_attribute(self):
        self.instance_of()["Socket_12"]["attribute_name"] = "unused_saved_name"
        parameter = self.call("get_parameter", object="P", modifier="GN",
                              identifier="Socket_12")["parameter"]
        self.assertEqual(parameter["attribute_mode"]["mode"], "VALUE")

    def test_empty_name_does_not_select_value(self):
        self.instance_of()["Socket_12"]["type"] = 2
        parameter = self.call("get_parameter", object="P", modifier="GN",
                              identifier="Socket_12")["parameter"]
        self.assertEqual(parameter["attribute_mode"]["mode"], "ATTRIBUTE")

    def test_name_without_enum_proof_is_refused(self):
        del self.instance_of()["Socket_12"].bl_rna
        before = self.instance_of()["Socket_12"].to_dict()
        self.error("unsupported_mode", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_12", mode="ATTRIBUTE", attribute_name="position")
        self.assertEqual(self.instance_of()["Socket_12"].to_dict(), before)
        self.error("unsupported_mode", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_12", value=3.0)

    def test_enum_disallows_attribute_even_if_name_key_exists(self):
        self.instance_of()["Socket_12"].bl_rna.properties["type"].enum_items = [
            types.SimpleNamespace(identifier="VALUE", value=1)]
        self.error("unsupported_mode", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_12", mode="ATTRIBUTE", attribute_name="position")

    def test_silent_selector_corruption_rolls_back(self):
        before = self.instance_of()["Socket_12"].to_dict()
        self.blend.corrupt_hook = lambda identifier, key, value: 1 if key == "type" else value
        failure = self.error("write_verification_failed", "set_parameter", object="P", modifier="GN",
                             identifier="Socket_12", mode="ATTRIBUTE", attribute_name="position")
        self.assertTrue(failure.details["rolled_back"])
        self.assertEqual(self.instance_of()["Socket_12"].to_dict(), before)

    def test_dynamic_visibility_rejects_write(self):
        modifier = self.blend.objects.get("P").modifiers.get("GN")
        modifier.is_input_visible = lambda identifier: identifier != "Socket_12"
        self.error("hidden_input", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_12", value=3.0)
        report = self.call("get_parameter", object="P", modifier="GN",
                           identifier="Socket_12")["parameter"]
        self.assertFalse(report["writable"])
        self.assertFalse(report["attribute_writable"])

    def test_rna_dot_path_driver_blocks_mode_write(self):
        obj = self.blend.objects.get("P")
        obj.animation_data = AnimationData([DriverCurve('modifiers["GN"].properties.inputs.Socket_12.type')])
        failure = self.error("read_only_driven", "set_parameter", object="P", modifier="GN",
                             identifier="Socket_12", value=3.0)
        self.assertEqual(failure.details["drivers"][0]["matched"], "exact")

    def menu_with_real_value_map(self):
        instance = self.instance_of()["Socket_14"]
        instance["value"] = 10
        instance.bl_rna = types.SimpleNamespace(properties={"value": types.SimpleNamespace(
            type="ENUM", enum_items=[types.SimpleNamespace(identifier="FIRST", name="First", value=10),
                                     types.SimpleNamespace(identifier="SECOND", name="Second", value=42)])})
        return instance

    def test_noncontiguous_numeric_enum_is_not_a_menu_index(self):
        instance = self.menu_with_real_value_map()
        report = self.call("get_parameter", object="P", modifier="GN", identifier="Socket_14")["parameter"]
        self.assertEqual(report["enum"]["source"], "modifier_input_value_rna_enum")
        self.assertEqual(report["enum"]["identifiers"], ["FIRST", "SECOND"])
        self.call("set_parameter", object="P", modifier="GN", identifier="Socket_14", value=42)
        self.assertEqual(instance["value"], 42)
        self.error("value_out_of_range", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_14", value=1)
        self.assertEqual(instance["value"], 42)

    def test_menu_identifier_maps_to_actual_numeric_value(self):
        instance = self.menu_with_real_value_map()
        self.call("set_parameter", object="P", modifier="GN", identifier="Socket_14", value="SECOND")
        self.assertEqual(instance["value"], 42)

    def test_numeric_menu_without_value_map_is_refused(self):
        item = next(item for item in self.group.interface.items_tree if item.identifier == "Socket_14")
        for entry in item.bl_rna.properties["default_value"].enum_items:
            del entry.value
        report = self.call("get_parameter", object="P", modifier="GN", identifier="Socket_14")["parameter"]
        self.assertFalse(report["writable"])
        self.error("unsupported_enum", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_14", value=1)
        self.assertEqual(self.value_of("Socket_14"), 0)
