"""Operation-domain registration without changing existing tool definitions.

Mixed legacy modules (notably scene.py) are registered through a small
decorator proxy. Each known function is registered in its one declared domain;
its original SDK decorator arguments, schema and callable remain unchanged.
Unknown future functions are still recorded, so every startup selection rejects
an incomplete domain mapping rather than silently expose or drop them.
"""
from __future__ import annotations

from ..tool_profiles import TOOL_DOMAINS, DOMAIN_GLOBAL
from . import (drivers, file_open, geometry_nodes, launcher, meta, node_frames,
               node_notes, operation_log, outliner, outliner_management, preview, scene, shape_keys)
from . import properties, properties_iteration, model_inspection, geometry_nodes_edit, geometry_nodes_capture
from . import review_maintenance


class _DomainRegistrar:
    def __init__(self, mcp, domain, names):
        self.mcp, self.domain, self.names = mcp, domain, names

    def tool(self, *args, **kwargs):
        def decorate(fn):
            name = kwargs.get("name") or fn.__name__
            owner = TOOL_DOMAINS.get(name)
            if owner is not None and owner != self.domain:
                return fn
            if name in self.names:
                return fn
            result = self.mcp.tool(*args, **kwargs)(fn)
            self.names.add(name)
            return result
        return decorate


def _register(mcp, client, names, domain, modules):
    registrar = _DomainRegistrar(mcp, domain, names)
    for module in modules:
        register = getattr(module, "register", None)
        if register is None:
            register = module.register_outliner_tools
        register(registrar, client)


def register_core_tools(mcp, client, names):
    _register(mcp, client, names, DOMAIN_GLOBAL, (meta, launcher, scene, file_open, review_maintenance))


def register_outliner_tools(mcp, client, names):
    _register(mcp, client, names, "outliner", (outliner, outliner_management, scene))


def register_geometry_node_tools(mcp, client, names):
    _register(mcp, client, names, "geometry_nodes", (geometry_nodes, geometry_nodes_edit, geometry_nodes_capture))


def register_properties_tools(mcp, client, names):
    _register(mcp, client, names, "properties", (properties, properties_iteration))


def register_model_inspection_tools(mcp, client, names):
    _register(mcp, client, names, "model_inspection", (model_inspection,))


def register_object_modeling_tools(mcp, client, names):
    _register(mcp, client, names, "object_modeling", (scene,))


def register_viewport_tools(mcp, client, names):
    _register(mcp, client, names, "viewport", (preview, scene))


def register_shape_key_tools(mcp, client, names):
    _register(mcp, client, names, "shape_keys", (shape_keys,))


def register_driver_tools(mcp, client, names):
    _register(mcp, client, names, "drivers", (drivers,))


def register_node_frame_tools(mcp, client, names):
    _register(mcp, client, names, "node_frames", (node_frames,))


def register_node_note_tools(mcp, client, names):
    _register(mcp, client, names, "node_notes", (node_notes,))


def register_scene_evaluation_tools(mcp, client, names):
    _register(mcp, client, names, DOMAIN_GLOBAL, (scene,))


def register_audit_tools(mcp, client, names):
    _register(mcp, client, names, DOMAIN_GLOBAL, (operation_log,))


REGISTRATIONS = (register_core_tools, register_outliner_tools,
    register_geometry_node_tools, register_object_modeling_tools,
    register_viewport_tools, register_shape_key_tools, register_driver_tools,
    register_node_frame_tools, register_node_note_tools,
    register_scene_evaluation_tools, register_audit_tools,
    register_properties_tools, register_model_inspection_tools)
