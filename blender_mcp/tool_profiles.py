"""Explicit tool classification and initial DIRECTORY views.

Every registered tool is permanently exposed/callable. Profiles and domain
selection only choose which interface documentation the directory returns.
"""
from __future__ import annotations
import asyncio
import os
import json
from typing import Any, Iterable, Mapping

PROFILE_ENV_VAR = "BLENDER_MCP_PROFILE"
LOCAL_DOMAINS_ENV_VAR = "BLENDER_MCP_LOCAL_DOMAINS"
VIEW_DOMAINS_ENV_VAR = "BLENDER_MCP_VIEW_DOMAINS"
DEFAULT_PROFILE = "global-only"
DOMAIN_CORE = "global"
DOMAIN_LOG_AUDIT = "global"
DOMAIN_OUTLINER = "outliner"
DOMAIN_OBJECT_MODELING = "object_modeling"
DOMAIN_GEOMETRY_NODES = "geometry_nodes"
DOMAIN_SHAPE_KEYS = "shape_keys"
DOMAIN_DRIVERS = "drivers"
DOMAIN_NODE_FRAMES = "node_frames"
DOMAIN_NODE_NOTES = "node_notes"
DOMAIN_VIEWPORT = "viewport"
DOMAIN_PROPERTIES = "properties"
DOMAIN_MODEL_INSPECTION = "model_inspection"

#: Every tool the current build registers belongs to exactly one of these
#: domains. The order is the documentation order used by ``enabled_domains``.
DOMAIN_GLOBAL = "global"
# Legacy identifiers are aliases of the common documentation category.
DOMAIN_CORE = DOMAIN_GLOBAL
DOMAIN_LOG_AUDIT = DOMAIN_GLOBAL
DOMAIN_SCENE_EVAL = DOMAIN_GLOBAL
DOMAIN_TOOLS: dict[str, tuple[str, ...]] = {
    # Session, bridge diagnostics, scene/file/object inventory and saving. Small
    # on purpose: everything here is either read-only or part of the documented
    # one-case-per-file workflow. Object discovery (``blender_list_objects``)
    # lives here for directory organization; every profile exposes every tool.
    DOMAIN_CORE: (
        "blender_health",
        "blender_server_status",
        "blender_list_commands",
        "blender_ping",
        "blender_bridge_probe",
        "blender_environment",
        "blender_launch_blender",
        "blender_reload_review_bridge",
        "blender_list_scenes",
        "blender_scene_summary",
        "blender_list_objects",
        "blender_object_info",
        "blender_create_case_scene",
        "blender_switch_scene",
        "blender_save_blend",
        "blender_save_scene",
        "blender_inspect_blend_file",
        "blender_open_blend",
        "blender_operation_log",
        "blender_log_dashboard",
        "blender_set_frame",
        "blender_update_depsgraph",
        "blender_tool_domains",
        "blender_tool_domain_state",
        "blender_set_tool_domains",
    ),
    # Collection organisation and the ordinary Object/Collection lifecycle: the
    # fixed outliner family - hierarchy, paths, linking, moving, unlinking,
    # visibility, plus collection create/rename/move/delete, object
    # create/rename/delete and the collection/object display switches.
    #
    # ``blender_outliner_object_*`` manage ordinary Objects in a collection (like
    # ``bpy.data.objects`` entries); they are NOT Blender "Collection Instance"
    # objects (an Empty whose instance_collection points at a collection).
    DOMAIN_OUTLINER: (
        "blender_outliner_hierarchy",
        "blender_outliner_ensure_path",
        "blender_outliner_link_objects",
        "blender_outliner_move_objects",
        "blender_outliner_unlink_objects",
        "blender_outliner_set_visibility",
        "blender_outliner_collection_create",
        "blender_outliner_collection_rename",
        "blender_outliner_collection_move",
        "blender_outliner_collection_delete",
        "blender_outliner_object_create",
        "blender_outliner_object_rename",
        "blender_outliner_object_delete",
        "blender_outliner_collection_display",
        "blender_outliner_object_display",
    ),
    # Named-object construction primitives needed to build a case from scratch.
    DOMAIN_OBJECT_MODELING: (
        "blender_create_mesh_object",
        "blender_create_primitive",
        "blender_set_custom_property",
    ),
    # Editable Geometry Nodes graphs and modifiers.
    DOMAIN_GEOMETRY_NODES: (
        "blender_create_node_group",
        "blender_create_node",
        "blender_set_node_property",
        "blender_set_socket_default",
        "blender_link_sockets",
        "blender_unlink_socket",
        "blender_remove_node",
        "blender_inspect_node_group",
        "blender_attach_geometry_nodes_modifier",
        "blender_set_modifier_input",
        "blender_list_node_types",
        "blender_describe_node_type",
        "blender_batch_graph_edit",
        "blender_geometry_nodes_layout",
        "blender_geometry_nodes_move_nodes",
        "blender_geometry_nodes_reroute_insert",
        "blender_geometry_nodes_reroute_move",
        "blender_geometry_nodes_reroute_delete",
        "blender_geometry_nodes_capture_editor",
    ),
    DOMAIN_SHAPE_KEYS: (
        "blender_inspect_shape_keys",
        "blender_create_shape_key",
        "blender_set_shape_key_coordinates",
        "blender_set_shape_key",
        "blender_remove_shape_key",
    ),
    DOMAIN_DRIVERS: (
        "blender_inspect_drivers",
        "blender_add_driver",
        "blender_update_driver",
        "blender_remove_driver",
    ),
    # Native node canvas Frames and their internal Text datablocks.
    DOMAIN_NODE_FRAMES: (
        "blender_node_frame_create",
        "blender_node_frame_get",
        "blender_node_frame_parent",
        "blender_node_frame_unparent",
        "blender_node_frame_update",
        "blender_node_frame_set_text",
        "blender_node_frame_layout",
    ),
    # Optional Mod node-notes extension. Never required by the other domains.
    DOMAIN_NODE_NOTES: (
        "blender_node_note_status",
        "blender_node_note_extension",
        "blender_node_note_create",
        "blender_node_note_get",
        "blender_node_note_update",
        "blender_node_note_delete",
    ),
    # GUI viewport: preview rendering and explicit object presentation
    # (``blender_present_object`` changes the 3D View selection/framing, so it is
    # a viewport operation, not an outliner one).
    DOMAIN_VIEWPORT: (
        "blender_capture_preview",
        "blender_present_object",
    ),
    DOMAIN_PROPERTIES: (
        "blender_properties_list_parameters",
        "blender_properties_get_parameter",
        "blender_properties_set_parameter",
        "blender_properties_set_parameters",
        "blender_properties_iterate_parameters",
    ),
    DOMAIN_MODEL_INSPECTION: (
        "blender_model_inspection_inspect",
        "blender_model_inspection_capture_viewport",
    ),
}


DOMAINS = tuple(DOMAIN_TOOLS)
LOCAL_DOMAINS = tuple(d for d in DOMAINS if d != DOMAIN_GLOBAL)
TOOL_DOMAINS: dict[str, str] = {}
for domain, names in DOMAIN_TOOLS.items():
    if not names:
        raise RuntimeError(f"empty domain: {domain}")
    for name in names:
        if name in TOOL_DOMAINS or not name.startswith("blender_"):
            raise RuntimeError(f"duplicate/invalid classified tool: {name}")
        TOOL_DOMAINS[name] = domain
GLOBAL_TOOL_NAMES = frozenset(DOMAIN_TOOLS[DOMAIN_GLOBAL])
CONTROL_TOOL_NAMES = frozenset({"blender_tool_domains", "blender_tool_domain_state", "blender_set_tool_domains"})
KNOWN_TOOL_NAMES = frozenset(TOOL_DOMAINS)
NEW_OUTLINER_TOOL_NAMES = OUTLINER_TOOL_NAMES = frozenset(DOMAIN_TOOLS[DOMAIN_OUTLINER])
NEW_REVIEW_TOOL_NAMES = frozenset(n for n in KNOWN_TOOL_NAMES if n.startswith((
    "blender_properties_", "blender_model_inspection_", "blender_geometry_nodes_")))
MAINTENANCE_TOOL_NAMES = frozenset({"blender_reload_review_bridge"})
EXISTING_TOOL_NAMES = KNOWN_TOOL_NAMES - NEW_OUTLINER_TOOL_NAMES - NEW_REVIEW_TOOL_NAMES - MAINTENANCE_TOOL_NAMES - CONTROL_TOOL_NAMES

class ToolProfileError(Exception):
    pass
class ToolProfileValueError(ToolProfileError, ValueError):
    pass
class ToolProfileConfigurationError(ToolProfileError, RuntimeError):
    pass
class ToolProfileApplicationError(ToolProfileError, RuntimeError):
    pass

def view_tool_names_for_domains(domains: Iterable[str]) -> frozenset[str]:
    selected = tuple(domains) or (DOMAIN_GLOBAL,)
    unknown = set(selected) - set(DOMAINS)
    if unknown:
        raise ToolProfileValueError(f"unknown directory domains: {sorted(unknown)}")
    return frozenset(n for d in selected for n in DOMAIN_TOOLS[d])

# Compatibility helper: this computes a directory view, not a permission set.
tool_names_for_domains = view_tool_names_for_domains

class ToolProfile:
    def __init__(self, name: str, domains: Iterable[str], description: str):
        domains = tuple(domains)
        self.name = name
        self.domains = tuple(d for d in DOMAINS if d in domains)
        if set(domains) - set(DOMAINS):
            raise ToolProfileValueError(f"unknown local domain in profile {name}")
        self.description = description
        self.tool_names = KNOWN_TOOL_NAMES
        self.view_tool_names = view_tool_names_for_domains(self.domains)
    @property
    def tool_count(self):
        return len(self.tool_names)
    @property
    def is_all(self):
        return self.name == "all"
    @property
    def restricted(self):
        return False
    def describe(self):
        return dict(profile=self.name, domains=list(self.domains), tool_count=self.tool_count,
                    description=self.description, restricted=False, view_tool_count=len(self.view_tool_names), all_tools_active=True)

PROFILE_DOMAINS = {
    "global-only": (), "core": (), "all": DOMAINS,
    "outliner": (DOMAIN_OUTLINER,),
    "geometry_nodes": (DOMAIN_GEOMETRY_NODES, DOMAIN_OBJECT_MODELING),
    "algorithm_reproduction": (DOMAIN_OUTLINER, DOMAIN_GEOMETRY_NODES, DOMAIN_OBJECT_MODELING),
    "algorithm_review": (DOMAIN_PROPERTIES, DOMAIN_GEOMETRY_NODES, DOMAIN_MODEL_INSPECTION),
}
PROFILES = {n: ToolProfile(n, d, "Initial tool-directory view; every registered tool remains available.")
            for n, d in PROFILE_DOMAINS.items()}
PROFILE_NAMES = tuple(PROFILES)
ALGORITHM_REVIEW_TOOL_NAMES = PROFILES["algorithm_review"].tool_names

def resolve_profile(profile=None, environ: Mapping[str, str] | None = None) -> ToolProfile:
    if isinstance(profile, ToolProfile):
        # Do not trust forged/custom profile names or partial-domain allowlists.
        if profile.name == "explicit-domains" and profile.tool_names == KNOWN_TOOL_NAMES:
            return profile
        if profile.name not in PROFILES or profile.tool_names != PROFILES[profile.name].tool_names:
            raise ToolProfileValueError("custom/partial profile is unsupported")
        return profile
    source = os.environ if environ is None else environ
    view_key = VIEW_DOMAINS_ENV_VAR if VIEW_DOMAINS_ENV_VAR in source else LOCAL_DOMAINS_ENV_VAR
    if profile is None and view_key in source:
        try:
            domains = json.loads(source[view_key])
        except (ValueError, TypeError) as exc:
            raise ToolProfileValueError(f"{view_key} must be a JSON array") from exc
        if (not isinstance(domains, list) or len(domains) > len(DOMAINS) or
                any(not isinstance(d, str) or d not in DOMAINS for d in domains) or
                len(domains) != len(set(domains))):
            raise ToolProfileValueError(f"{view_key} requires unique classified directory IDs")
        if PROFILE_ENV_VAR in source:
            resolve_profile(source[PROFILE_ENV_VAR], {})
        return ToolProfile("explicit-domains", domains, "Explicit initial directory view; all tools available.")
    raw = profile if profile is not None else source.get(PROFILE_ENV_VAR, DEFAULT_PROFILE)
    if not isinstance(raw, str) or not raw.strip() or raw.strip().lower() not in PROFILES:
        raise ToolProfileValueError(f"unknown/empty tool profile {raw!r}; supported: {PROFILE_NAMES}")
    return PROFILES[raw.strip().lower()]

def configuration_source(profile=None) -> str:
    if profile is not None:
        return "explicit_argument"
    if VIEW_DOMAINS_ENV_VAR in os.environ:
        return VIEW_DOMAINS_ENV_VAR
    if LOCAL_DOMAINS_ENV_VAR in os.environ:
        return LOCAL_DOMAINS_ENV_VAR
    return PROFILE_ENV_VAR if PROFILE_ENV_VAR in os.environ else "default"

def enabled_domains(profile=None):
    """Legacy helper returning initial view categories, not enabled capabilities."""
    return resolve_profile(profile).domains

def allowed_tool_names(profile=None):
    return resolve_profile(profile).tool_names

def describe_profiles():
    return {n: p.describe() for n, p in PROFILES.items()}

def validate_registered(names: Iterable[str]) -> frozenset[str]:
    values = list(names)
    if any(not isinstance(n, str) or not n for n in values) or len(values) != len(set(values)):
        raise ToolProfileConfigurationError("invalid/duplicate registered tool names")
    registered = frozenset(values)
    unknown, missing = registered - KNOWN_TOOL_NAMES, KNOWN_TOOL_NAMES - registered
    if unknown or missing:
        raise ToolProfileConfigurationError(f"unclassified registered tools={sorted(unknown)}; missing classified tools={sorted(missing)}")
    return registered

def registered_tool_names(mcp):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return frozenset(t.name for t in asyncio.run(mcp.list_tools()))
    raise ToolProfileApplicationError("use await mcp.list_tools() in an active event loop")

class AppliedProfile:
    """Startup metadata only. Runtime state is queried with the global state tool."""
    def __init__(self, profile, registered, kept, removed):
        self.profile = profile
        self.profile_name = profile.name
        self.domains = profile.domains
        self.restricted = profile.restricted
        self.registered_count = len(registered)
        self.kept = frozenset(kept)
        self.kept_count = len(kept)
        self.removed = tuple(removed)
        self.removed_count = len(removed)
    def describe(self):
        return dict(profile=self.profile_name, initial_view_domains=list(self.domains),
                    registered_count=self.registered_count, visible_count=self.kept_count,
                    directory_view_count=len(self.profile.view_tool_names),
                    all_tools_active=True, physically_removed_count=0)

def apply_tool_profile(mcp, profile=None, registered_names=None):
    """Select an initial directory view; never filter registration or invocation.

    A bare FastMCP has no directory controller and cannot apply a view.
    """
    selected = resolve_profile(profile)
    controller = getattr(mcp, "domain_controller", None)
    if controller is None:
        raise ToolProfileConfigurationError("tool-directory server required; bare FastMCP has no directory controller")
    validate_registered(controller.registered_names if registered_names is None else registered_names)
    controller.configure_initial(selected, configuration_source(profile))
    return AppliedProfile(selected, controller.registered_names, selected.tool_names,
                          ())
