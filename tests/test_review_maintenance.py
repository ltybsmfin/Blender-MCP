"""Fixed module reload boundaries and resident state preservation."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import unittest
from unittest.mock import patch
from tests.test_geometry_nodes_capture import Failure

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('review_maintenance_test',ROOT/'blender/review_maintenance.py')
maintenance=importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.api=NS(__file__=str(ROOT/'blender/blender_mcp_addon.py'),CommandError=Failure,
                    _COMMANDS={},_SERVER=object())
        def register(name,**metadata):
            def decorate(fn):
                self.api._COMMANDS[name]=fn
                return fn
            return decorate
        self.api.command=register
        maintenance.register_commands(self.api)
        self.fn=self.api._COMMANDS['reload_review_commands']
        self.modules={n:NS(__name__=n,__file__=str(ROOT/'blender'/(n+'.py')),
                           register_commands=lambda api:None) for n in maintenance.MODULES}
        self.modules['review_capture_common']._SESSIONS={}
        self.modules['review_capture_common']._janitor=lambda:None
        self.bpy=NS(data=NS(filepath='original.blend'),context=NS(scene=NS(name='Original')),
                    app=NS(timers=NS(is_registered=lambda fn:False)))
    def invoke(self, reload):
        with patch.dict(sys.modules,{'bpy':self.bpy}), \
             patch.object(maintenance.importlib,'import_module',side_effect=lambda n:self.modules[n]), \
             patch.object(maintenance.importlib,'reload',side_effect=reload):
            return self.fn()
    def test_fixed_modules_preserve_bridge_project_and_scene(self):
        server=self.api._SERVER
        result=self.invoke(lambda m:m)
        self.assertEqual(result['refreshed_modules'],list(maintenance.MODULES))
        self.assertIs(server,self.api._SERVER)
        self.assertFalse(result['saved_blend'])
        self.assertEqual(self.bpy.data.filepath,'original.blend')
        self.assertEqual(self.bpy.context.scene.name,'Original')
    def test_import_outside_bridge_refused_before_any_reload(self):
        self.modules['properties'].__file__='C:/unrelated/properties.py'
        reloads=[]
        with self.assertRaises(Failure) as caught:
            self.invoke(lambda m:reloads.append(m))
        self.assertEqual(caught.exception.code,'module_origin_mismatch')
        self.assertEqual(reloads,[])
    def test_failure_restores_command_registry_and_is_not_success(self):
        before=dict(self.api._COMMANDS)
        def bad(module):
            self.api._COMMANDS['unexpected']=object()
            raise RuntimeError('fixture import failure')
        with self.assertRaises(Failure) as caught:
            self.invoke(bad)
        self.assertEqual(caught.exception.code,'review_reload_failed')
        self.assertEqual(before,self.api._COMMANDS)
        self.assertFalse(caught.exception.details['module_globals_rolled_back'])
    def test_no_arbitrary_code_or_module_arguments(self):
        with self.assertRaises(TypeError):
            self.fn(code='print(1)')


if __name__=='__main__':
    unittest.main()
