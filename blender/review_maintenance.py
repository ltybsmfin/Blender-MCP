"""Reload only the fixed review bridge modules; no code/path argument channel."""
import importlib
from pathlib import Path
import os

MODULES = ('review_capture_common', 'properties', 'geometry_nodes_edit',
           'model_inspection', 'geometry_nodes_capture')


def register_commands(api):
    @api.command('reload_review_commands', mutating=True,
                 summary='reload the five fixed review modules, preserving the bridge and project')
    def reload_review_commands():
        directory = Path(api.__file__).resolve().parent
        imported = []
        # Validate every import origin before executing any reload.
        for name in MODULES:
            module = importlib.import_module(name)
            expected = directory / (name + '.py')
            if Path(module.__file__).resolve() != expected:
                raise api.CommandError('module_origin_mismatch',
                    'refusing to reload a module outside the fixed bridge directory',
                    details={'module':name, 'expected':str(expected)})
            imported.append(module)
        import bpy
        server = getattr(api, '_SERVER', None)
        command_snapshot = dict(api._COMMANDS)
        common = imported[0]
        for session in list(common._SESSIONS.values()):
            common.restore_session(session, reason='review_module_reload')
        original_file = bpy.data.filepath
        original_scene = bpy.context.scene.name
        timers = getattr(bpy.app, 'timers', None)
        if timers and timers.is_registered(common._janitor):
            timers.unregister(common._janitor)
        refreshed=[]
        try:
            for module in imported:
                module=importlib.reload(module)
                if module.__name__ != 'review_capture_common':
                    module.register_commands(api)
                refreshed.append(module.__name__)
            if (getattr(api, '_SERVER', None) is not server or bpy.data.filepath != original_file
                    or bpy.context.scene.name != original_scene):
                raise RuntimeError('bridge or project state unexpectedly changed')
        except Exception as exc:
            api._COMMANDS.clear()
            api._COMMANDS.update(command_snapshot)
            raise api.CommandError('review_reload_failed', str(exc), details={
                'refreshed_modules':refreshed, 'command_registry_restored':True,
                'module_globals_rolled_back':False, 'saved_blend':False}) from exc
        return {'refreshed_modules':refreshed,'pid':os.getpid(),
                'bridge_preserved':True,'blend_filepath':original_file,'scene':original_scene,
                'saved_blend':False,'registered_commands':sorted(api._COMMANDS)}
