# Outliner / 集合（Collection DAG）工具

本文说明新增的六个 `blender_outliner_*` 工具：集合层级查询、幂等路径、对象链接/移动/解除链接、
集合显示标志。所有写操作只改**归属关系**，从不删除 Object 或 Collection，也不碰
`object.data`、变换、修改器、几何节点组、材质与连线。

模块边界：

| 文件 | 角色 |
| --- | --- |
| `blender/outliner.py` | Blender 侧桥命令，`register_commands(api)`，只 `import bpy`，不 import `mcp` |
| `blender_mcp/tools/outliner.py` | MCP 侧六个 typed 工具，`register_outliner_tools(mcp, bridge)`（另有 `register` 别名），**不 import `bpy`** |
| `tests/test_outliner.py` | 纯内存 fake `bpy` + fake `api` 的回归；MCP 侧用假 bridge 校验 schema 与转发 |
| `docs/OUTLINER.md` | 本文 |

桥命令前缀 `outliner_*`：`outliner_hierarchy`、`outliner_ensure_path`、`outliner_link_objects`、
`outliner_move_objects`、`outliner_unlink_objects`、`outliner_set_visibility`。

---

## 1. 原生 API 依据（Blender 5.2.1 LTS）

| 事实 | 依据 |
| --- | --- |
| 场景有独立根集合，用 `Scene.collection` 取得 | [Scene API](https://docs.blender.org/api/5.2/bpy.types.Scene.html)；本仓库既有代码 `_guarded_collection_target()` 已用 `scene.collection` 与 `scene.collection.children_recursive`（实机测试通过） |
| 集合可被多个父集合/多个场景引用，层级是 **DAG** 而不是目录树 | [Collection API](https://docs.blender.org/api/5.2/bpy.types.Collection.html) 的 `children`；本仓库 `_scene_roster()` 用 `scene.collection.all_objects` / `children_recursive` 统计层级 |
| `Collection.children` 是 `CollectionChildren`（`link` / `unlink`），`Collection.objects` 是 `CollectionObjects`（`link` / `unlink`） | 同上；既有 `create_mesh_object` 已用 `scene.collection.objects.link(obj)` |
| 对象可属于多个集合，反向视图是 `Object.users_collection` | Collection/Object API；本服务用它做“全局归属”读取 |
| `Collection.hide_viewport` / `Collection.hide_render` 是集合自身的原生显示标志 | Collection API；与 `Object.hide_viewport` / `Object.hide_render`（视口/渲染中单个对象的标志）是两组不同属性 |
| `Collection.library` / `Collection.override_library` / `ID.is_library_indirect` 标识链接/覆盖数据 | 既有 `_is_linked_or_overridden()` |
| 几何节点组是 `bpy.data.node_groups` 里的**全局**数据块，不属于任何集合 | 既有 `_DRIVER_ID_COLLECTIONS["NODETREE"] = "node_groups"`；它没有任何 collection 归属关系 |

### 为什么不直接复用既有辅助函数

* `get_object(name)` 走 `bpy.data.objects.get(name)`，在同名（本地 + linked）时**静默取第一个**。
  本模块要求“精确且唯一”，因此自己扫描并区分 `object_not_found` / `ambiguous_object`；
  存在多个同名数据块时拒绝而不是猜。
* `_guarded_collection_target()` 是**基于 active scene** 的写保护（`bpy.context.scene`）。
  本模块按需求不依赖任何 UI 上下文，改为按 `scene` 参数建索引：`in_scene`（集合属于哪些场景）、
  `parent_map`（全文件直属父集合），并据此判定 `collection_shared_across_scenes` /
  `collection_multi_parent` / `collection_outside_scene`。

设计上刻意不做的事：**不使用** `bpy.context`（不读 active scene / view layer / window
manager），**不调用** 任何 operator；因此 GUI 与 `--background` 行为一致，也不受用户当前
选择影响。`tests/test_outliner.py` 会扫描源码禁止 `bpy.context.` / `bpy.ops.` / `bpy.types.`，
并让 fake `bpy` 完全没有 `context`/`ops` 属性，任何误用都会直接报错。

### 尚未在本机实机验证的部分（留给真实 MCP 验收）

本次改动**没有启动/连接 Blender**（工作约束），因此下面几点是“按文档实现并做了防御”，
需要用真机确认后再视情况收紧：

1. `bpy.data.collections` **是否包含** 各场景的 master collection。实现两边都兼容：
   `all_collections()` = 所有 `scene.collection` + `bpy.data.collections`，按 `as_pointer()` 去重。
   如果 5.2.1 确实把 master 也列在 `bpy.data.collections` 里，也没有重复或漏项。
2. `bpy.data.collections.new("名字")` 在同名数据块已存在时是否重命名为 `"名字.001"`。
   实现先做**全局同名检查**（`collection_name_conflict`，不造 `.001`），再在创建后
   读回 `Collection.name` 做兜底（不一致 → `invalid_name` 并删除刚创建的数据块）。
3. `CollectionChildren.link()` 对“把自己挂到自己的后代下”是否报错。查询侧的环检测不依赖它：
   路径枚举按**祖先链**判环，`children` 里出现重复路线只算 DAG（`multi_parent`），不算环。
4. `Collection.hide_viewport` / `hide_render` 写入后立即读回是否稳定。实现写入后读回校验，
   不一致报 `outliner_write_failed`。
5. Blender 的 ID 名长度上限（`MAX_ID_NAME`）。实现允许到 255 UTF-8 字节，靠**读回比对**
   发现截断/改名，因此不会把“被改名的结果”当成功。
6. `bpy.data.collections.remove()` 之后访问该数据块的 `name` / `as_pointer()` / `children`
   会抛 `ReferenceError`。本次按审查结论实现（移除前缓存、之后只用缓存值），并在 fake 中
   完全建模；真机验收请确认该失效行为与清理路径的实际表现。

---

## 2. 路径语义

* 路径是 `/` 分隔的**精确名称**序列，从 `Scene.collection` 出发，每段必须是上一段的
  **直属子集合**（`children`），不是全局按名字找。
* `""`（空串）只在“定位**已存在**的根集合”时代表场景根：`outliner_hierarchy` 用 `scene` 参数
  取根，`link_objects` / `move_objects` / `unlink_objects` / `set_visibility` 的 `path=""` 就是根；
  `outliner_ensure_path` **要求至少一段**（`path=""` → `invalid_path`）。
* 拒绝：前后斜线（`/A`、`A/`）、空段（`A//B`）、反斜线（`A\B`，歧义）、点段（`.`、`..`）、
  段首尾空白、超长（段 > 255 字节、整条 > 1024 字节、段数 > 64）。
* 名称含 `/` 的集合无法用路径表达（会被拆成两段而 `collection_not_found`）——这是路径语法的
  固有限制，已在此说明。
* 解析到**多个同名直属子集合** → `ambiguous_collection`（不猜）；解析不到 → `collection_not_found`
  （details 带 `parent` / `resolved` / `available`）。

---

## 3. DAG 语义与写保护

集合是 DAG：一个集合可以有多个**直属父集合**，也可以同时属于多个场景。查询会如实报告：

* `parents` / `parent_count` / `multi_parent`：全文件范围内的全部直属父集合；
* `scene_paths`：**该场景内**从根到它的每条路径（上限内）；`scenes` / `cross_scene`：所有包含它的场景；
* `cycles`：真正的环（同一集合出现在自己的祖先链上）。**多父共享不会被误报成环**；
* `truncated` / `graph_truncated` / `paths_truncated` / `scene_index_truncated`：命中遍历上限时明确置位。

写保护（`link` / `unlink` / `move` 的目标与来源，以及 `ensure_path` 的父、`set_visibility` 的目标）：

| 情况 | 结果 |
| --- | --- |
| 集合 `library` / `override_library` / `is_library_indirect` | `read_only_collection` |
| 集合属于多于一个场景（含经由共享祖先进入） | `collection_shared_across_scenes` |
| 集合或其祖先有多个直属父集合 | `collection_multi_parent` |
| 集合不属于该场景 | `collection_outside_scene` |
| 遍历索引被截断（无法证明单一归属） | `index_truncated` |
| 场景本身是链接/覆盖数据（写操作） | `read_only_scene` |

理由：这些改动会从**另一条层级路线或另一个场景**被看见，属于超出本次命名路径的副作用，
因此默认拒绝（六个工具的签名里没有 force/allow 开关，不提供绕过）。

`ensure_path` 采用**先预检、后写入**两阶段（不在写入过程中才发现问题）：

1. **阶段 1（只读规划整条路径）**：逐段解析；缺失段只登记为**虚拟段**——绝不把它当成真实集合，
   也绝不拿它当场景根（不会 `children_of(None)`）；同时检查
   * `create_missing=false` 且有缺段 → `collection_not_found`；
   * 路径内部重复名字（如 `New/New`）→ `collection_name_conflict`，`reason=duplicated_inside_path`；
   * 全局同名异位置 → `collection_name_conflict`，`reason=exists_elsewhere`（绝不接受 `.001`）。
2. **阶段 2（只读归属守卫）**：对**根集合**与路径上**每一个已存在的段**（含末尾被复用的那一段）
   逐个做 §3 的守卫 → linked / cross-scene / multi-parent 一律在**写入之前**失败。
3. **阶段 3（写入）**：只有前两阶段全部通过才 `bpy.data.collections.new()` + `children.link()`。

因此：预检失败时 `bpy.data.collections.new` 与 `bpy.data.collections.remove` 调用次数都是 **0**，
原结构（已有集合、父关系、对象归属）不变；阶段 3 的**真实**分配/链接失败才做清理，且只清理
**本次调用自己创建且仍无外部使用者**的数据（无对象、无子集合、无额外父、非链接数据），
在 `details.cleanup` 里如实给出 `removed` / `kept` / `failed` / `clean`。

**移除顺序（RNA 失效）**：`bpy.data.collections.remove()` 之后数据块的 RNA 即失效，
再读 `name` / `as_pointer()` / `children` 会抛 `ReferenceError`。清理逻辑因此在移除**之前**
缓存名字，之后只使用缓存值，连 `failed` 错误条目也用缓存名（不会二次异常）。
`tests/test_outliner.py` 的 fake 与 Blender 一致：移除后访问上述任一属性都会抛 `ReferenceError`。

幂等：已存在的段放进 `reused`，只创建缺失段（`created`），重复调用 `created=[]`、
`idempotent=true`，且**不产生任何 new/remove 调用**。因为是整条路径预检，成功返回时
`writable` 恒为 `true`、`write_guard` 为最终目标的守卫结果；不可写路径现在是**失败**而不是
“返回 writable=false 却仍创建”。不创建对象、不改任何归属。

---

## 4. 六个工具

### `blender_outliner_hierarchy(scene, include_memberships=true)`

只读。返回该场景的集合 DAG（每个路径一行：`name`/`path`/`parents`/`scenes`/`scene_paths`/
`objects`/`children`/`linked`/`override`/`multi_parent`/`cross_scene`），以及 `cycles`、
`truncated`、`limits`。`include_memberships=true` 时另给每个场景对象的全局归属
（`membership.collections[].{collection,scenes,scene_paths,outside_scene}`、`orphan`、
`linked_object`），并把**不属于任何集合**的对象单独放在 `collectionless_objects`
（正是 `unlink` 的 `orphan` 保护对象）。`global_data.node_groups` 明确说明几何节点组是全局数据、
不是集合成员。绝不使用 active scene。

`view_layers` 返回该 Scene 的精确视图层名称，另有 `view_layer_count` 和
`view_layers_truncated`。调用显示工具时使用返回的名称，不猜测默认 `ViewLayer`。

### `blender_outliner_ensure_path(scene, path, create_missing=true)`

整条路径**先只读预检、后写入**（见 §3）：根与每个已存在段（含末尾复用段）的归属守卫、
`create_missing=false` 的缺段、路径内重复名字与全局同名都在写入前判定，失败时 **0 new / 0 remove**。
幂等：已存在的段放进 `reused`，只创建缺失段（`created`），重复调用 `created=[]`、`idempotent=true`
且不再调用 new/remove。成功返回 `prechecked=true`、`writable=true` 与 `write_guard` 诊断信息。
不创建对象、不改任何归属。

### `blender_outliner_link_objects(scene, path, objects)`

把精确对象名链入目标集合。**整批先校验**：未知对象 `object_not_found`、重复名
`duplicate_object`、全局重名（本地与 linked 同名）`ambiguous_object` —— 校验失败时**一个链接都不加**。
已经在目标集合里的对象进 `skipped`（幂等）。默认**只加不减**：不解除其它归属，不动
`object.data`/变换/修改器/GN 组/材质/连线，不删除任何数据块。返回每个对象 `before` / `after` 归属。

### `blender_outliner_move_objects(scene, path, objects, unlink_from)`

`unlink_from` **必填**（`[]` 表示只链接，等价于 link），元素是**同一场景**的路径（`""` = 根）。
只解除显式列出的来源；未列出的归属（包括另一个场景的集合）一律不碰。
先算出整批结果再动手：`path` 出现在 `unlink_from` → `conflicting_paths`；重复来源 →
`duplicate_path`；结果会让对象不属于任何场景 → `orphan_object`（未改动任何东西，返回 `planned`）。
已在目标集合中的对象不重复链接。

### `blender_outliner_unlink_objects(scene, path, objects, confirm_orphan=false)`

只断开“对象 ↔ 该集合”这一条关系。未链在该集合的对象进 `skipped`（幂等）。
**整批预计算**：若某对象在批次结束后不属于任何集合、或不属于任何场景 → 默认
`orphan_object` 拒绝且不改动；`confirm_orphan=true` 时按请求解除，对象数据块**保留**并在
`warnings` / `orphan_objects` 里告警。任何情况下都不删除对象，`objects_deleted` 恒为 `[]`。

### `blender_outliner_set_visibility(scene, path, hide_viewport=null, hide_render=null)`

写 `Collection.hide_viewport` / `Collection.hide_render`。至少给一个（两个都是 `null` →
`invalid_argument`），未给的一个保持不变，非布尔（严格 `bool`）→ `invalid_argument`。
读写回校验、返回 `before`/`after`，重复调用 `changed=false`（幂等）。
只动这两个集合标志，不动对象自身的 `hide_viewport`/`hide_render`。

---

## 5. 稳定错误码

| 码 | 含义 |
| --- | --- |
| `invalid_scene` / `scene_not_found` / `ambiguous_scene` | 场景名缺失/不存在/重名 |
| `invalid_path` | 路径语法（空串用于 ensure_path、前后斜线、空段、反斜线、点段、首尾空白、超长） |
| `collection_not_found` / `ambiguous_collection` | 路径解析不到 / 同名直属子集合多于一个 |
| `collection_name_conflict` | 全局同名异位置（`reason=exists_elsewhere`）或同一路径内重复请求（`reason=duplicated_inside_path`）：创建会被 Blender 改名为 `.001`，拒绝（`auto_suffix=false`，0 new/0 remove） |
| `invalid_name` | Blender 把新建集合存成了别的名字（截断/改名），已删除刚创建的数据块 |
| `invalid_objects` / `object_not_found` / `ambiguous_object` / `duplicate_object` | 对象列表参数与精确名字解析 |
| `read_only_collection` / `read_only_scene` | 链接或覆盖数据，永不写入 |
| `collection_shared_across_scenes` / `collection_multi_parent` / `collection_outside_scene` | 副作用越界，默认拒绝 |
| `conflicting_paths` / `duplicate_path` | move 的目标=来源 / 来源重复 |
| `orphan_object` | 批次会让对象不属于任何集合或场景（需 `confirm_orphan=true`） |
| `index_truncated` | 遍历上限导致无法证明单一归属，拒绝写入 |
| `invalid_argument` | 严格布尔/类型参数不合法 |
| `api_unsupported` / `scene_master_unavailable` | 该 Blender 版本缺少所需 RNA |
| `outliner_write_failed` | 变更中途失败；details 给出 `applied`/`compensated`/`residual`/`rolled_back`，**不伪装完全回滚** |

---

## 6. 上限（响应里的 `limits`，每个遍历都有界）

| 项 | 值 |
| --- | --- |
| 路径字节 / 段数 / 单段字节 | 1024 / 64 / 255 |
| 每次调用对象数 / `unlink_from` 条数 | 512 / 64 |
| 每场景集合数 / 全文件集合数 / 全文件边数 | 2000 / 5000 / 20000 |
| 路径深度 / 每集合报告的路径数 | 32 / 8 |
| 每集合直属对象 / 对象行数 / 每对象归属数 | 200 / 2000 / 32 |

命中上限时：查询置 `truncated`（并按需置 `graph_truncated`/`paths_truncated`/
`scene_index_truncated`）；写操作只在**安全相关**（父集合/场景归属）不完整时以
`index_truncated` 拒绝，纯粹“路径报告”截断仍允许写入（深场景不会被锁死）。

---

## 7. MCP 侧（SDK 1.30.0 正式参数）

六个工具都用官方参数 `name` / `title` / `description` / `annotations` / `meta`：

```python
@mcp.tool(
    name="blender_outliner_hierarchy",
    title="Outliner: collection hierarchy and object ownership",
    description="...",
    annotations=ToolAnnotations(title=..., readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
    meta={"domain": "outliner", "bridge_prefix": "outliner_", "mutating": False,
          "idempotent": True, "deletes_data": False},
)
```

* `readOnlyHint`：只有 `hierarchy` 为 `True`。
* `destructiveHint`：`move_objects` / `unlink_objects` 为 `True`（会移除归属关系，属“非纯增量”提示），
  其余为 `False`；六个工具都 **不删除任何数据块**（`deletes_data: false`）。
* `meta.domain = "outliner"`，与 `blender_mcp/tool_profiles.py` 的 `DOMAIN_OUTLINER`
  （含这六个固定名字）一致，供 profile 过滤使用。
* 旧的工具 schema 未改动：本模块只新增六个工具，注册仍走 `domains.REGISTRATIONS`，
  `blender_mcp/tools/__init__.py`、`tool_profiles.py` 由核心集成方维护。
* 参数由 SDK 生成严格 schema：`objects` 为 `minItems=1 / maxItems=512` 的精确名字数组，
  `path.maxLength=1024`，`scene.maxLength=255`，布尔为 `strict=True` 的 `bool`/`bool|null`。

---

## 8. 本次验证（可复现）

```powershell
# 1) fake bpy / fake api 的回归（不启动 Blender，不读写 Project）
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe D:\AI\MCP\Blender\MCP\tests\test_outliner.py
# 2) 核心集成（domains/profile）下的工具清单
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe -c "import asyncio,sys; sys.path.insert(0,'.'); ..."
```

实测结果（本次会话）：

* `tests/test_outliner.py`：**53 项通过**（`Ran 53 tests ... OK`）。
  覆盖：DAG 多父（`parents`=2、`cycles=[]`、两条 `scene_paths`）、场景根与集合同名
  （`collection_name_conflict`，不产生 `.001`）、跨场景共享与多父拒绝、真环终止并上报、
  同名直属子集合歧义、深层级截断但仍可写、幂等 ensure_path/link/move/unlink、
  整批先校验、注入链接/解除失败后的**补偿**与诚实 details、`orphan` 默认拒绝与
  `confirm_orphan=true` 保留对象、对象/集合不被删除、`object.data`/修改器/GN 组快照不变、
  前后归属、严格参数（bool/name/list/path 语法）、`hide_viewport`/`hide_render` 读写回与幂等、
  源码不含 `bpy.context.`/`bpy.ops.`/`eval(`/`exec(`。
  其中本次修复新增/加强：晚段全局同名与路径内重复名字的**预检 0 new/0 remove**、
  复用段（含中间段）linked/shared/multi-parent 拒绝、完整幂等（重复调用不新增/不删除）、
  移除后 RNA 失效（`name`/`as_pointer`/`children`/`objects` 抛 `ReferenceError`）下
  cleanup 仍 `clean`、结构完整恢复、remove 异常按缓存名如实上报 `failed`、
  成员写操作 0 new/0 remove 且既有对象/集合身份与快照不变。
* 直接行为探针（fake，本次实测输出）：
  * 晚段全局同名 `One/Two/Props` → `collection_name_conflict`（`reason=exists_elsewhere`），
    `new_calls=0 remove_calls=0`，`names=['Props']`，`root children=['Props']` 不变；
  * 路径内重复 `New/New` → `collection_name_conflict`（`reason=duplicated_inside_path`），同样 0/0；
  * 复用段的守卫：`Props`（多父）与 `Props/Deep` → `collection_multi_parent`；
    `Shared`/`Shared/Deep`（两场景共享）→ `collection_shared_across_scenes`；
    `Linked`/`Linked/Deep`（链接数据）→ `read_only_collection`，全部 0/0；
  * 幂等：首次 `ensure_path A/B/C` 计数器 `(new=3, remove=0)`，重复调用
    `created=[] reused=['A','B','C']` 且计数器仍为 `(3, 0)`、`idempotent=true`；
  * 注入链接失败 → `cleanup={'removed': ['Third','Y','X'], 'kept': [], 'failed': [], 'clean': True}`；
  * 移除后 RNA：`name` / `as_pointer` / `children` / `objects` 全部抛
    `ReferenceError: StructRNA of type Collection has been removed`，cleanup 仍 `clean` 且结构完整恢复；
  * remove 异常（注入）时 `failed` 条目用缓存名如实上报（`clean=false`），不触发二次 ReferenceError。
* MCP 侧：在 `build_server(profile="all")` 下共 **67 个工具**，六个 `blender_outliner_*` 全部出现；
  `profile="outliner"` 下同样包含六个，且不含 `blender_create_node_group`；`title` / `annotations`
  / `meta.domain == "outliner"` 均按 SDK 1.30.0 正式参数生效。
* 相关既有套件：`test_audit_features.py` 30 项通过、`test_review_transport.py` 通过、
  `test_node_frames_atomic.py` 通过。`test_unit_validation.py` 有 3 个**既有**沙箱失败
  （`PermissionError [WinError 5]` 写同级 `Project\`；见 `tests/run_all_tests.ps1` 头注释），
  与本次改动无关。
* 环境说明：`tests/reports/stdio_feature_report.json` 当前是一份 **3.6 秒的失败报告**
  （`blender_pid=132756`，`mcp_stdio_feature_session` 失败，写入时间 2026-10-05 00:24），
  不是有效的验收结果；该目录被 `.gitignore` 忽略，真实验收需由核心集成方在实机上重跑。

**未做**：真实 Blender 5.2.1 中的实机验收（本次工作约束禁止启动/连接 Blender）。
第 1 节列出的 5 项原生行为需要由真实 MCP 验收在有界夹具场景里确认；在那之前，
本文不声称“已实机通过”。

### 接线清单（核心集成方）

1. **Blender 侧桥命令尚未注册**：`blender/blender_mcp_addon.py` 目前只注册
   `node_frames` / `node_notes_adapter` / `file_open`。需要按同样方式追加
   `outliner.register_commands(sys.modules[__name__])`（例如新增
   `_register_outliner_commands()`），六个 `outliner_*` 桥命令才会在 Blender 里可用。
   本模块不在可写文件范围内，未代为修改。
2. MCP 侧已由 `blender_mcp/tools/domains.py` 的 `register_outliner_tools` 接入
   `build_server`（实测 `profile="all"` 67 工具、`profile="outliner"` 含这六个且不含 GN 工具）。
3. 已知需要随本次接入更新的既有断言（不属于本模块可写文件）：
   * `tests/test_node_frames.py::SchemaTests.test_existing_schemas_unchanged` 写死的工具总数
     61 应为 **67**（实测 `AssertionError: 67 != 61`）。
   * `tests/test_tool_profiles.py` 的两个用例（空/未知 profile 期望退出码 3）当前实测为 1：
     `build_server()` 抛出的 `ToolProfileValueError` 没有被转成退出码（fixture 服务器侧），
     与 outliner 模块无关（异常发生在 `resolve_profile`，早于任何工具注册）。
