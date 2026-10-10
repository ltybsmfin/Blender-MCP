# Scene isolation and independent per-scene export

Status: implemented in `blender/blender_mcp_addon.py` (bridge commands
`create_case_scene`, `switch_scene`, `list_scenes`, `save_scene`,
`inspect_blend_file`) and exposed as typed MCP tools in
`blender_mcp/tools/scene.py`. Acceptance runner:
`tests/test_scene_isolation.py` (`--live`).

## What this solves

Case work used to happen in whatever scene the running Blender had loaded, and
`blender_save_blend` saved the **whole live file** with `save_as_mainfile`
(re-pointing the session at the new `.blend`).  Both are unsafe for a case
workstream that must not disturb the pre-existing cases in the file.

The isolation workstream adds:

* a **case scene** that starts genuinely empty and can be activated and verified;
* a **per-scene export** that writes exactly one scene and its mandatory
  dependency closure into a standalone `.blend`, without saving or re-pointing
  the live main file;
* a **read-only inspector** for `.blend` files under `Project`;
* a **guarded collection target** so a new object can never be linked into a
  collection that belongs to another scene.

## Tools

| MCP tool | Bridge command | Mutating |
| --- | --- | --- |
| `blender_create_case_scene(name, activate=true)` | `create_case_scene` | yes |
| `blender_switch_scene(name)` | `switch_scene` | yes (context only) |
| `blender_list_scenes(include_objects=false)` | `list_scenes` | no |
| `blender_save_scene(scene, relative_path, overwrite=false, compress=false)` | `save_scene` | yes (writes a new file) |
| `blender_inspect_blend_file(relative_path)` | `inspect_blend_file` | no |

`blender_save_blend`, `blender_scene_summary`, `blender_list_objects` and the
rest of the earlier surface keep their old semantics.

### `blender_create_case_scene`

* `bpy.data.scenes.new(name)` — a brand new scene, no objects / collections /
  world copied from any existing scene.
* The name must be globally unique.  An existing name is an error
  (`already_exists`) and is **never auto-suffixed**: an accidental `Case.001`
  would silently split case data.
* Emptiness is *verified* (`objects`, `collection.all_objects`,
  `children_recursive`, and - if a world exists - that it is not a pre-existing
  world datablock).  If Blender ever created something else, the new scene is
  removed again and the call fails.
* Every pre-existing scene is fingerprinted (objects, collections, world, view
  layers, frame range) before and after; any difference aborts and rolls the new
  scene back (`scene_isolation_violation`).  With `activate=true` the roster is
  verified a **second** time after the switch, so activation itself can never
  slip a change past the check.
* `activate=true` performs an explicit `window.scene = <scene>` and **proves**
  it: the window must have kept the assignment *and* `bpy.context.scene` must be
  the target scene.  A missing window, a missing context scene
  (`context_unavailable`) or a context that stays on the old scene
  (`scene_switch_unverified`) fails closed; the switch is rolled back, the new
  scene is removed, and the response reports whether the rollback actually
  succeeded (`rollback: {attempted, restored, window_scene}`) instead of claiming
  a restore that did not happen.  Success is never faked.
* Returns `previous_scene`, `current_scene`, `stable_id`
  (`ID.session_uid`, falling back to the datablock address), the emptiness
  counts, the unchanged pre-existing scene list and the window report.

### `blender_switch_scene`

Same explicit, verified `window.scene` assignment, plus a scene-roster
comparison that refuses to report a switch that changed scene data; on such a
failure the previous window scene is restored again and the actual rollback
result is reported.  The already-current case is proven the same way (a `None`
context scene fails closed rather than being treated as success).

### `blender_list_scenes`

Read-only inventory: name, stable id, `is_current`, object count, collection
count, world, frame range and which GUI windows show the scene.  With
`include_objects=true` it also returns the exact object names per scene, which
is how "the other scenes were not touched" is verified.

### `blender_save_scene`

Export **one** scene, never the whole live file:

1. **Path checks on both sides** — the MCP tool validates the path with
   `blender_mcp.config.resolve_project_path` (rejects absolute paths, `..`,
   symlink/junction components, anything outside `<workspace>/Project`), requires
   a `.blend` suffix, and refuses an existing file unless `overwrite=true`.
   The Blender side repeats all of it through its own copy of the same rules.
2. **No main-file write** — the scene is written with explicit, non-default
   arguments, never with `save_as_mainfile`:
   `bpy.data.libraries.write(stage, {scene}, fake_user=False, compress=...,
   path_remap='ABSOLUTE')`.  `path_remap='ABSOLUTE'` keeps references to files
   that live outside the exported `.blend` (images, fonts, sounds, other
   resources in the closure) absolute, so they still resolve after the file is
   published into a different directory.  **External assets are not packed**:
   the command deliberately does not call `bpy.ops.file.pack_all`, so the result
   is a reference-only export (the payload reports `external_assets_packed:
   false`) and those files are neither copied nor embedded.
   `bpy.data.filepath`, the current scene, the context and the preferences are
   fingerprinted before staging **and again immediately before publishing**, and
   must be unchanged (`live_state_changed` otherwise, with the delta reported).
3. **Dependency guard before writing** — `bpy.data.user_map()` maps every ID to
   its users; inverting it yields the dependency closure of the scene, **the
   target scene included** (it is written too, so it must appear in the proven
   manifest; the master collection is marked as scene-owned/embedded).  The
   export is refused (`external_dependency`) when the closure contains
   * another scene (`external_scene`),
   * an object outside the scene's own objects (`external_object`) — e.g. a
     driver or Geometry Nodes reference pulling an old case in,
   * a collection outside the scene's own collection hierarchy
     (`external_collection`),
   * linked-library data or a library override where standalone safety is not
     proven (`external_library`) — including the root scene itself.
   If `user_map()` does not report the scene's own objects as dependencies, the
   export is refused (`dependency_guard_unavailable`) rather than exporting
   unverified data.
4. **Staging, not publishing** — the file is first written to an unguessable
   name (`.mcp-scene-stage.<32 hex>.blend`) in the *target's own directory*, so
   publication is a same-volume rename/link.  The staged file must exist, be at
   least `MIN_STAGED_BLEND_BYTES`, and carry a recognizable container signature
   (`BLENDER`, gzip or zstd).  Filesystem failures while reading it become a
   recoverable `scene_export_failed`.
5. **Read-only verification of the staged file** — through a temporary main:
   `with bpy.data.temp_data() as temporary: with
   temporary.libraries.load(stage) as (data_from, data_to): ...`.
   `bpy.data.temp_data` is a context-manager **factory**
   (`temp_data(*, filepath=None)`) whose temporary `BlendData` is freed when the
   `with` block exits; it is called as a factory and its `libraries` are only
   touched inside that context.  *All* ID-type name lists the file exposes are
   read, and nothing is ever assigned to `data_to`, so no datablock of the staged
   file enters the live `bpy.data` and the staged file is never opened as the
   session file.  The verdict must be:
   * exactly one scene, the requested one (`staged_scene_mismatch`),
   * exactly the intended object inventory (`staged_objects_missing`,
     `staged_objects_unexpected`),
   * every staged ID proven by the closure / the scene's own hierarchy
     (`unexpected_datablock`),
   * every expected dependency whose ID type the metadata route *does* expose
     present in the file (`dependency_missing`) — a lost node group, mesh,
     material or action is a hard failure, not a warning.
   Only two kinds of absence are tolerated, and both are reported explicitly in
   the payload: `embedded` (datablocks that are owned by the scene, such as the
   master collection, or whose ID type the file metadata does not expose as a
   linkable inventory) and `unmapped` (a closure ID whose `bpy.data` collection
   could not be identified — reported so it can be investigated, never silently
   dropped).
6. **Atomic publish** — the destination is re-validated after staging and the
   live state is checked once more immediately before publishing.  With
   `overwrite=true` the staged file replaces the target via `os.replace` (atomic
   on Windows/NTFS, and the old file survives any earlier failure).  With
   `overwrite=false` publication uses an **atomic no-replace** primitive only:
   `os.link` (a hard link in the target's own directory), with `os.rename` on
   Windows as the second choice because it also refuses an existing destination.
   There is deliberately **no non-atomic copy fallback**: if neither primitive is
   available the publish is refused with `publish_unsupported`, because a copy
   that failed halfway would leave a truncated `.blend` under the final name.
   A target that appeared meanwhile yields `file_exists` and is kept.  After
   publishing, the final file must exist and be exactly the size of the verified
   staging file (`publish_failed` otherwise), so a mixed or truncated `.blend`
   can never look like a success.  Only the staging file this command created is
   ever deleted, in a `finally`.

### `blender_inspect_blend_file`

Returns scenes, objects, libraries, every ID-type inventory and per-type counts
of a `.blend` under `Project`, plus `opened_into_live_data: false`.  Same
temp-data route, no `bpy.ops.wm.open_mainfile`, no change to the live file path
or current scene.

### Guarded collection creation (`blender_create_mesh_object`)

A new mesh object is only ever linked into the **active scene's own** collection
hierarchy (the scene's master collection or one of its descendants).  Passing a
collection that belongs to another scene (or to no scene) is refused with
`collection_outside_scene`; a collection that is part of the active scene but is
*also* linked into another scene is refused with `collection_shared_across_scenes`
(putting a case object there would change that scene too).  The target is
resolved *before* any datablock is allocated, so a rejected call leaves no mesh
or object behind.  The default path (`collection=None`, i.e. the active scene's
master collection) and all same-scene, single-scene collection names keep working
exactly as before.

## Isolation model and its limits — read this before case work

The new scene is **not a sandbox for the older named-data tools**.  Every other
tool (`blender_create_node_group`, `blender_set_custom_property`,
`blender_add_driver`, `blender_create_shape_key`, ...) still resolves names in
the *whole file*, so a caller can still reach data that belongs to another
scene.  The contract is therefore:

1. **Use globally unique, prefixed names** for everything created for a case
   (scene, objects, meshes, node groups, collections, actions).  The acceptance
   runner uses `mcpiso_<random>` as the prefix and
   `_MCP验收_mcpiso_<random>` as the folder.
2. **Never reuse or replace a pre-existing name.**  Name collisions are what
   would let case work edit old case data (the older tools resolve by name; some
   accept `if_exists="replace"` / `replace=true`).
3. **Export before you reference outside the scene.**  `blender_save_scene`
   refuses an export whose closure leaves the scene; that refusal is the
   intended signal that a driver, modifier or custom-property reference points
   at another case.
4. Nothing in this workstream deletes datablocks.  Case scenes and exported
   files stay in place after an acceptance run.

## Error codes added

| code | meaning |
| --- | --- |
| `already_exists` | a scene with that exact name already exists (never suffixed) |
| `scene_not_found` | no such scene in the file |
| `context_unavailable` | no window at all / no context scene: a switch cannot be proven |
| `scene_switch_failed` | `window.scene` assignment failed or was not kept |
| `scene_switch_unverified` | the window changed but `bpy.context.scene` did not follow; rolled back |
| `scene_creation_failed` | the new scene was not what was requested (non-empty, wrong name, reused world) |
| `scene_isolation_violation` | creating/switching a scene changed data outside it |
| `scene_rollback_failed` | the freshly created scene could not be removed |
| `external_dependency` | closure leaves the scene (details list each problem) |
| `dependency_guard_unavailable` | `user_map()` unusable or not reporting the scene's objects |
| `api_unsupported` | `libraries.write` / callable `temp_data()` factory / `user_map` unavailable, or the explicit `libraries.write` arguments were rejected |
| `scene_export_failed` | `libraries.write` failed or produced an implausible/unreadable staged file |
| `blend_inspect_failed` | the temp-data metadata read failed |
| `staged_scene_mismatch` / `staged_objects_missing` / `staged_objects_unexpected` / `unexpected_datablock` | staged-content verification failed; nothing was published |
| `dependency_missing` | an expected dependency whose ID type the metadata can expose is absent from the staged file |
| `file_exists` | target exists / appeared and `overwrite=false` (old file kept) |
| `publish_failed` | the atomic publish or its size/read-back verification failed (old file kept) |
| `publish_unsupported` | no atomic no-replace primitive is available; nothing was published (no copy fallback) |
| `live_state_changed` | the export/inspection changed live state (delta reported) |
| `invalid_path`, `file_not_found`, `collection_outside_scene`, `collection_shared_across_scenes`, `collection_not_found` | as before / the collection guard |

## Verifying it

2026-10-04 本机验收：Blender 5.2.1 LTS / `127.0.0.1:9876`，22 项针对性单元与 schema
检查、74 项真实 MCP 检查全部通过。补充完整工作流验证 8 项通过：原对象详情、原节点图、原场景记录、
工程路径和活动场景保持不变；第二案例单独导出不含第一案例；移除故意引入的跨案例驱动器后可以恢复保存。
`Project/chairs` / `Project/models` 中 4 个已有 `.blend` 的 SHA256 前后相同。

推荐完整验收（240 秒整体限制；内部单套验证 180 秒）：

```powershell
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe D:\AI\MCP\Blender\MCP\tests\test_case_workflow_acceptance.py
```

补充报告在 `.runtime/scene_acceptance_original_state.json`。本次独立文件示例：
`Project/算法复现/00_算法基础/_MCP验收_mcpiso_0376438a/isolated.blend`（第一案例），
`next_case.blend`（第二案例，62,913 字节、压缩），`recovered.blend`（第一案例移除外部引用后，170,073 字节）。
读取文件后只确认 metadata 暴露的可链接 ID 清单；形态键等 mesh-owned/embedded 数据并不单独出现在
`libraries.load` 名称清单中，保存结果在 `manifest.embedded` 中明确报告。不得把这个清单表述为逐顶点的形态键验证。

API 依据：[Blender 5.2 libraries.write/load](https://docs.blender.org/api/5.2/bpy.types.BlendDataLibraries.html)、
[temp_data/user_map](https://docs.blender.org/api/5.2/bpy.types.BlendData.html)、
[Window.scene](https://docs.blender.org/api/5.2/bpy.types.Window.html)。

Non-Blender (no bridge, no Blender process):

```
<workspace>\MCP\.venv\Scripts\python.exe MCP\tests\test_scene_isolation.py
```

Live acceptance against **an already running** Blender (the runner never starts
or stops Blender; the token is read by the MCP server itself and never printed):

```
<workspace>\MCP\.venv\Scripts\python.exe MCP\tests\test_scene_isolation.py --live
```

The live runner creates two uniquely prefixed case scenes, a small object, a
Geometry Nodes group, a shape key and a self-contained driver, exports
`Project/算法复现/00_算法基础/_MCP验收_<unique>/isolated.blend`, inspects it through
MCP, proves the original scenes and the main-file path are unchanged, exercises
the rejection matrix (overwrite, path escape, missing scene, duplicate scene
name, external-object dependency) and finally switches back to the original
scene.  Reports land in `MCP/.runtime/scene_isolation_report.json` and
`MCP/.runtime/logs/scene_isolation_<utc>.json`.

## Runtime API dependencies (Blender 5.2)

The pipeline is refused (never silently weakened) when any of these is missing:

* `bpy.data.libraries.write()` with the explicit `fake_user`/`compress`/
  `path_remap` arguments — the only writer used;
* `bpy.data.temp_data`, callable as a context-manager factory
  (`with bpy.data.temp_data() as temporary: temporary.libraries.load(...)`) —
  read-only metadata of a staged file;
* `bpy.data.user_map()` — the dependency closure;
* `os.link` or (on Windows) `os.rename` for an atomic no-replace publish;
* `ID.session_uid` (falls back to the datablock address for the stable id).

If a check that should pass fails on the real build (for example
`unexpected_datablock` or `dependency_missing` naming a datablock you believe is
part of the scene, or `scene_switch_unverified` on a real GUI session), that is a
concrete blocker to report to the workstream owner: the code deliberately fails
closed instead of publishing unverified data.
