"""Exact scene evaluation and evaluated mesh lifetime, using a fake RNA API."""
import importlib
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from tests.test_geometry_nodes_capture import AddonCase, FakeCollection, FakeObject, FakeScene, Failure


class Identity(list):
    def __init__(self):
        super().__init__([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]])
    def __matmul__(self, value):
        return value


class InspectionTests(AddonCase):
    def setUp(self):
        super().setUp()
        self.obj = FakeObject('Fixture')
        self.mesh = NS(name='FixtureMesh', bl_rna=NS(identifier='Mesh'),
                       vertices=[NS(co=[0,0,0]), NS(co=[2,3,4])],
                       edges=[], polygons=[], loops=[], materials=[])
        self.obj.data = self.mesh
        self.obj.matrix_world = Identity()
        self.obj.evaluated_get = lambda deps: self.obj
        self.obj.to_mesh = lambda: self.mesh
        self.clears = 0
        self.obj.to_mesh_clear = lambda: setattr(self, 'clears', self.clears+1)
        self.scene.objects.append(self.obj)
        self.layer = self.bpy.window.view_layer
        self.layer.objects.append(self.obj)
        self.scene.view_layers = [self.layer]
        self.bpy.data.objects.append(self.obj)
        self.bpy.data.scenes = FakeCollection([self.scene])
        self.bpy.data.meshes = [self.mesh]
        self.bpy.data.curves = []
        self.deps = NS(scene=self.scene, update=lambda:None, object_instances=[])
        self.bpy.context.evaluated_depsgraph_get = lambda:self.deps
        sys.modules.pop('model_inspection', None)
        self.module = importlib.import_module('model_inspection')
        with patch.object(self.module, '_numpy', return_value=None):
            self.module.register_commands(self.api)

    def inspect(self, **params):
        return self.api.commands['model_inspect'](object='Fixture', **params)

    def test_counts_world_bounds_and_temporary_mesh_cleanup(self):
        data = self.inspect(scene='Main')
        self.assertEqual(data['counts']['vertices'], 2)
        self.assertEqual(data['bounds']['size'], [2,3,4])
        self.assertEqual(data['components'], ['MESH'])
        self.assertEqual(self.clears, 1)
        self.assertTrue(data['state_unchanged']['unchanged'])

    def test_dependency_graph_instance_rows_are_consumed_before_next_step(self):
        class EphemeralRow:
            valid = True
            @property
            def parent(self):
                if not self.valid:
                    raise ReferenceError('StructRNA has been removed')
                return None
        def iterator():
            row = EphemeralRow()
            yield row
            row.valid = False
        self.deps.object_instances = iterator()
        data = self.inspect()
        self.assertEqual(data['instances']['errors'], [])
        self.assertEqual(data['instances']['instances'], 0)
        self.assertEqual(len(data['signature_sha256']['checksum']), 64)

    def test_nonmesh_instance_does_not_claim_complete_model_bounds(self):
        self.deps.object_instances = [NS(parent=self.obj, is_instance=True,
                                         instance_object=NS(name='Curve', type='CURVE'))]
        data = self.inspect()
        self.assertEqual(data['instances']['instances'], 1)
        self.assertEqual(data['instances']['bounds_unavailable_sources'], ['Curve'])
        self.assertFalse(data['instances']['complete'])
        self.assertFalse(data['bounds']['complete'])

    def test_wrong_scene_is_rejected_before_mesh_read(self):
        other = FakeScene('Other')
        self.bpy.data.scenes.append(other)
        with self.assertRaises(Failure) as caught:
            self.inspect(scene='Other')
        self.assertEqual(caught.exception.code, 'object_not_in_scene')
        self.assertEqual(self.clears, 0)

    def test_wrong_dependency_graph_is_never_used(self):
        self.deps.scene = FakeScene('Wrong')
        with self.assertRaises(Failure) as caught:
            self.inspect()
        self.assertEqual(caught.exception.code, 'depsgraph_scene_mismatch')
        self.assertEqual(self.clears, 0)

    def test_inactive_scene_override_does_not_change_window_scene(self):
        active = FakeScene('Active')
        self.bpy.window.scene = active
        self.bpy.context.scene = active
        data = self.inspect(scene='Main')
        self.assertEqual(data['scene']['name'], 'Main')
        self.assertIs(self.bpy.window.scene, active)
        self.assertIs(self.bpy.context.scene, active)

    def test_mesh_is_cleared_when_statistics_fail(self):
        self.mesh.vertices[0].co = [object(),0,0]
        with self.assertRaises((TypeError, ValueError)):
            self.inspect()
        self.assertEqual(self.clears, 1)


if __name__ == '__main__':
    unittest.main()
