# Outliner 管理包（Collection 生命周期 / 普通 Object / 视图层显示开关）

本文说明 `blender_outliner_management.py` 提供的**九个** `blender_outliner_*` 管理工具：
集合的创建/重命名/移动/删除、**普通 Mesh/Empty 对象**的创建/重命名/删除，以及集合与
对象的 Outliner 显示开关（小眼睛 / 复选框 / 监视器 / 渲染）。

这是第二个 Outliner 工作包，与既有 `docs/OUTLINER.md`（六个“组织类”工具）并列。
既有模块只改**归属关系**，从不删除数据；本模块是唯一允许**删除** Collection 与
Object 的入口，因此把“先全量校验、再写入、不可撤销的部分如实上报”写成硬约束。

## 0. 本次 delta 的关键纠正

| 项 | 结论 |
| --- | --- |
| “实例”的含义 | **普通网格对象（Mesh/Empty）**，即 `bpy.data.objects.new` 造出的对象。上一个版本的六个 `outliner_instance_*`（Collection Instance）命令、工具与文档已**全部移除** |
| 残留的实例逻辑 | 只保留 `outliner_collection_delete` 里的**只读安全扫描**：集合若被 Collection Instance（`Object.instance_collection`）当作 source，则删除该集合会被 `collection_in_use` 拒绝（`reason=collection_instance_source`） |
| 不新增的工具 | 不加 `object_info`（既有核心 `blender_object_info` 足够）、不加 `object_move`（既有 `outliner_move_objects` 支持普通对象）、不加 `object_transform`（Outliner 的移动是**归属改变**，3D 变换超出本包范围） |
| **监视器开关** | 之前的实现把 `disable_viewport` 当成“不支持”，**这是错的**。现在 `disable_viewport` = `Collection.hide_viewport` / `Object.hide_viewport`（全局监视器），与 `hidden`（视图层小眼睛）、`hide_render`（渲染）、`exclude`（集合复选框）四个开关**各自独立**、都必须支持 |
| 删除返回值 | 保留上一版的 RNA 修复（`cached_subtree` 缓存 name/key/父名，remove 之后绝不读死 RNA）与**纯 JSON 返回**（`parent` 返回名字而不是数据块指针，并有 `json.dumps` 往返测试），没有回退 |

模块边界：

| 文件 | 角色 |
| --- | --- |
| `blender/outliner_management.py` | Blender 侧 9 个桥命令，`register_commands(api, helpers)`，只 `import bpy` + 标准库，**不** import `mcp` |
| `blender_mcp/tools/outliner_management.py` | MCP 侧 9 个 typed 工具，`register_outliner_management_tools(mcp, bridge)`（另有 `register` 别名），**不 import `bpy`** |
| `tests/test_outliner_management.py` | 纯内存 fake `bpy` + fake `api` 的回归（复用并扩展 `tests/test_outliner.py` 的模型）；MCP 侧用假 bridge 校验 schema/转发/审计 |
| `tests/test_outliner_live_acceptance.py` | 由核心集成方准备的**真机**验收（本包不改动它，仅对其 9 个名字/参数/schema） |
| `docs/OUTLINER_MANAGEMENT.md` | 本文 |

桥命令前缀 `outliner_*`（= MCP 工具名去掉 `blender_`）：

| # | MCP 工具（参数） | 桥命令 |
| --- | --- | --- |
| 1 | `blender_outliner_collection_create(scene, parent_path, name)` | `outliner_collection_create` |
| 2 | `blender_outliner_collection_rename(scene, path, name)` | `outliner_collection_rename` |
| 3 | `blender_outliner_collection_move(scene, path, parent_path)` | `outliner_collection_move` |
| 4 | `blender_outliner_collection_delete(scene, path, confirm_delete, recursive=false)` | `outliner_collection_delete` |
| 5 | `blender_outliner_object_create(scene, path, name, type='MESH', vertices=null, faces=null)` | `outliner_object_create` |
| 6 | `blender_outliner_object_rename(scene, object, name)` | `outliner_object_rename` |
| 7 | `blender_outliner_object_delete(scene, objects, confirm_delete)` | `outliner_object_delete` |
| 8 | `blender_outliner_collection_display(scene, path, view_layer, hidden=null, disable_viewport=null, hide_render=null, exclude=null)` | `outliner_collection_display` |
| 9 | `blender_outliner_object_display(scene, objects, view_layer, hidden=null, disable_viewport=null, hide_render=null)` | `outliner_object_display` |

---

## 1. 复用既有原语（不是复制实现）

`blender/outliner_management.py` **不重新实现** `guard` / `resolve` / `run_batch` /
`write_target` / 索引等原语，而是从 `blender/outliner.py` 的闭包里取：

```python
helpers = outliner.register_commands(api)              # 拥有者模块已经 return 这个 dict
outliner_management.register_commands(api, helpers)     # 本模块复用同一批函数
```

必需助手键（缺失时**注册期直接 `RuntimeError`** 并列出缺的名字，绝不另写一份）：

```
fail, key, name_of, linked, scene_named, parse_path, boolean, object_names,
object_named, all_collections, index_for, build_index, resolve, guard, owners_of,
member_of, membership, row, run_batch, children_of, objects_of, children_rel,
objects_rel, master_of, write_target
```

`blender/outliner.py:register_commands(api)` 现在真的返回这个 dict（含
`move_objects` / `ensure_path` / `hierarchy`）。因此**测试不再需要 AST 注入 `return`**：
fixture 只把内存 stub `bpy` 装进 `sys.modules`，正常 import 两个模块、正常调用
`register_commands` 即可；测试断言返回值是 dict 且 25 个必需键齐全。
`move_objects` / `ensure_path` / `hierarchy` 本包不再使用（对象移动交给既有
`outliner_move_objects`），`write_target` 仍属共享契约的一部分。

`register_commands(api, helpers=None)` 在没拿到 helper dict 时（老接线）会尝试
`import outliner` 并取其 `register_commands(api)` 的返回值；仍拿不到就抛
`RuntimeError`，明确要求接线方传 helpers。

---

## 2. 原生 API 依据（Blender 5.2.1 LTS）

| 事实 | 依据 |
| --- | --- |
| 集合：`bpy.data.collections.new(name)` + `CollectionChildren.link/unlink`；本地集合名全文件唯一，撞名会静默变成 `name.001` | [Collection API](https://docs.blender.org/api/5.2/bpy.types.Collection.html)；既有 `outliner.py` 的同一调用与 `collection_name_conflict` 设计 |
| 对象：`bpy.data.objects.new(name, mesh_or_None)`；`name=None` 数据 → `EMPTY`，否则 `MESH` | [Object API](https://docs.blender.org/api/5.2/bpy.types.Object.html) |
| 网格：`bpy.data.meshes.new(name)` + `Mesh.from_pydata(vertices, edges, faces)`（`edges` 传空） | [Mesh API](https://docs.blender.org/api/5.2/bpy.types.Mesh.html) |
| `bpy.data.objects` 与 `bpy.data.meshes` 是**两个独立 ID 命名空间**：Object 与 Mesh 可以同名，两个 Mesh 不可以 | 两个 registry 各自唯一化；本模块对两者分别预检，都不接受 `.001` |
| `bpy.data.objects.remove(obj, do_unlink=True)` / `...collections.remove()` / `...meshes.remove()` 之后 RNA 失效（`name` / `as_pointer()` 抛 `ReferenceError`） | 既有 `cleanup_created` 的结论 + fake 完全建模；所有删除路径**先缓存名字** |
| 删除 Object **不会**释放它的数据：Mesh（及材质、GN 组）留在 `bpy.data` 里、users 归零 | Orphan data 只 `bpy.data.orphans_purge()`/存盘时才清；本模块从不 purge |
| `bpy.data.user_map(subset=[...])` 是官方“谁在使用这个 ID”的视图；`Object.users` 是引用计数 | 删除前的引用证明：显式关系（`Object.parent`、constraint 的 `target/object`、modifier 的 `object`、节点上的 `object`）+ `user_map` + `users >（直属 Collection 归属数）` 三重保护 |
| 显示开关：小眼睛 = `LayerCollection.hide_viewport`（集合）/ `Object.hide_set(hide, view_layer=...)`、`Object.hide_get(view_layer=...)`（对象）；复选框 = `LayerCollection.exclude`；**监视器 = `Collection.hide_viewport` / `Object.hide_viewport`**；渲染 = `Collection.hide_render` / `Object.hide_render`（相机同此） | [LayerCollection API](https://docs.blender.org/api/5.2/bpy.types.LayerCollection.html)、[Object API](https://docs.blender.org/api/5.2/bpy.types.Object.html) |

视图层解析**完全不使用 UI 上下文**：`scene.view_layers` 按名字精确匹配，
`LayerCollection` 从 `ViewLayer.layer_collection` 沿**集合路径**逐段下行，
再校验该节点确实包着同一集合（`key()` 比较）；不读 active view layer / window manager，
不调用任何 operator，也不切换活动场景。

---

## 3. 参数与拒绝规则

* 场景名、视图层名、集合/对象名都是**精确名字**；`scene` 永远显式，绝不使用 active scene。
* 路径语法沿用既有模块：`/` 分隔、`""` 仅代表已存在的场景根、拒绝前后斜线/空段/`.`/`..`/
  反斜线/首尾空白/超长。
* 集合名不允许含 `/`、`\`（否则再也无法用路径寻址）；对象名只要求非空、无 NUL、无首尾空白。
* 严格 `bool`：`type(value) is not bool` → `invalid_argument`；`confirm_delete`、`recursive`
  与四个显示开关都走这一条。
* 写对象前必须证明：显式属于该场景（`object_not_in_scene`）、只属于这一个场景
  （`object_shared_across_scenes`）、非链接/覆盖数据（`read_only_object`）。
* 显示工具：未给的开关保持原值（`null` = 保留），**全 `null` → `invalid_argument`**；
  `True`/`False` 分别表示“禁用/恢复”，逐开关读回。
* 名字撞车一律**拒绝**，绝不接受 Blender 的 `.001` 改名（`auto_suffix=false`）。

---

## 4. 九个工具

### 4.1 `blender_outliner_collection_create(scene, parent_path, name)`

在 `parent_path`（`""` = 场景根）下创建**一个**精确命名的集合：先解析父路径并做完整守卫
（同场景/单父/非链接）→ **全局同名预查**（同位置已存在则复用：`reused=[name]`、
`idempotent=true`、0 次 `new`；异位置存在 → `collection_name_conflict`，0 new/0 remove）
→ `bpy.data.collections.new` 后**读回**名字（被改名 → `invalid_name` 并删除刚建的数据块）
→ 通过共享 `run_batch` 链接（失败补偿并如实上报）→ 复核父子关系。
只创建一个集合数据块，不动对象/归属/显示开关/GN 组。

### 4.2 `blender_outliner_collection_rename(scene, path, name)`

改名：根 → `root_immutable`；目标先守卫；新名先全局预查（撞名 → `collection_name_conflict`）；
写入后读回，若 Blender 存成别的名字则**恢复原名**并报 `invalid_name`。
改名只影响寻址路径：对象的归属身份、`object.data`、修改器、GN 组、材质、连线都不变；
重复调用同名 `changed=false`。

### 4.3 `blender_outliner_collection_move(scene, path, parent_path)`

移动：源与目标父集合都解析 + 守卫，必须同场景；根不可移动；自我/后代目标 →
`cycle_hierarchy`。默认**先 link 新父、再 unlink 原父**（`run_batch` 反向补偿）；
当目标父是源的**祖先**时（例如把 `Props/Inner` 移到场景根），先 link 会临时产生同一路线上的
菱形，因此这一种情况改为**先 unlink 再 link**，补偿方向也相反。两种顺序都在
`details.applied/compensated/residual` 里如实上报。对象/数据/修改器/GN/材质/引用只被读取。

### 4.4 `blender_outliner_collection_delete(scene, path, confirm_delete, recursive=false)`

**永久删除**（Blender 侧不可撤销）。四阶段：

1. **只读预检（整个子树）**：`library`/`override` → `read_only_collection`；不在本场景 →
   `collection_outside_scene`；跨场景 → `collection_shared_across_scenes`；多父 →
   `collection_multi_parent`（同时覆盖“后代还被子树外的父集合引用”的 DAG 绕行）；外部父路线 →
   `collection_in_use(reason=external_parent)`；`use_fake_user` → `collection_in_use(fake_user)`；
   **被 Collection Instance 当 source** → `collection_in_use(collection_instance_source)`；
   被几何节点引用（node 的 `collection`，如 Collection Info）→
   `collection_in_use(geometry_nodes_reference)`；`Collection.users` 超过直属父数 →
   `collection_in_use(external_users)`。`confirm_delete=false` → `confirmation_required`；
   根 → `root_immutable`；默认模式还有子集合 → `collection_has_children`。
   预检失败时 **0 remove / 0 new**，`details.problems` 列出全部问题。
2. **对象保全规划（仍不写入）**：会脱离场景的对象**先挂到场景根**（不可重挂的链接数据 →
   `read_only_object` 拒绝整次删除）；有其它归属的对象**保持原归属**；同一对象即使在子树内
   多个集合里也**只规划一次**（否则会重复 link）。
3. **补偿式预链接**（`run_batch`，undo = 从根 unlink）。
4. **真正删除**：由深到浅（子树内相对深度），先 unlink 全部父关系再
   `bpy.data.collections.remove()`；**name/key/父名在删除前全部缓存**（remove 后 RNA 失效）。
   中途失败 → `outliner_delete_partial`（`deleted`/`residual`/`partial=true`/
   `not_transactional=true`/`rolled_back=false`），**不宣称事务**。

返回：`collections_deleted`、`subtree`、`preserved[]`、`relinked_to_root`、
`original_membership`/`membership_after`、`parent`（**名字**）、`parent_names[]`、
`siblings_kept`、`objects_deleted`（恒为 `[]`）、`warnings`。**绝不**删除对象数据块/网格/GN 组，
**不提供**对象级联删除，父/兄弟集合除 children 关系外不被修改。返回值纯 JSON（有测试）。

### 4.5 `blender_outliner_object_create(scene, path, name, type='MESH', vertices=null, faces=null)`

创建**一个普通对象**（不是 Collection Instance）：

* `type` 只能是 `'MESH'`（默认）或 `'EMPTY'`，其它 → `invalid_type`；
* `'EMPTY'` 带 `vertices`/`faces` → `invalid_argument`；`'EMPTY'` 只 `objects.new(name, None)`；
* `'MESH'`：`vertices`/`faces` 都可省略（**默认空 mesh**，供 GN 修饰器使用）；
  `vertices` 必须是**有限三元组**列表（`invalid_vertices`：非列表、长度≠3、含 `bool`/字符串/
  `NaN`/`∞`、超过 20000 个）；`faces` 必须是**整数下标**列表且每个面 ≥3 个角
  （`invalid_faces`：非列表、面 <3、下标不是 int/是 bool、下标越界、总角数超过 120000）；
  **全部先校验**，所以畸形请求一次 `bpy.data` 都不会碰；
* 名字：Object 名先做全文件预查（撞名 → `object_name_conflict`）；**Mesh 数据同名**冲突预查
  （→ `data_name_conflict`，`reason=mesh_name_exists_elsewhere`）。Object 与 Mesh 属不同 ID
  命名空间，所以 Object 与 Mesh 可以同名，两个 Mesh 不行。两者都**不接受 `.001`**，
  `data.name` 默认与对象同名；
* 分配顺序：`bpy.data.meshes.new` → 读回 mesh 名 → `Mesh.from_pydata(verts, [], faces)` +
  `Mesh.update()` → `bpy.data.objects.new(name, mesh)` → 读回对象名与 `Object.type` →
  `CollectionObjects.link` → 复核归属；
* 失败清理：只删除**本次调用自己创建的**数据块（先删对象再删 mesh；对象没删掉时 mesh 记为
  `residual` 保留），`details.cleanup` = `{object, data, removed[], residual[], errors[], clean}`；
* 返回：`scene` / `path` / `name` / `type` / `object{name,id,type,data,data_id,data_type,
  vertices,polygons,linked,override}` / `membership` / `collection` / `created` / `changed` /
  `idempotent=false` / `auto_suffix=false` / `warnings`（空 mesh 会提示需要 GN 生成几何）。
* 不切换活动场景、不使用 operator、不修改任何既有对象或数据块。

### 4.6 `blender_outliner_object_rename(scene, object, name)`

改名一个普通对象：对象必须显式属于本场景、单场景、非链接/覆盖（`object_not_in_scene` /
`object_shared_across_scenes` / `read_only_object`；未知/重名 → `object_not_found` /
`ambiguous_object`）。新名先全局预查（撞名 → `object_name_conflict`，不造 `.001`），写入后
读回；存成别的名字 → 恢复原名 + `invalid_name`。改名前后各取一次**引用快照**
（`data` 名与 id、修改器/`node_group`、shape keys、drivers 计数、vertex groups、constraints、
`parent`、`matrix_world`），若引用发生变化 → 恢复原名 + `outliner_write_failed`
（`references_preserved` 只在真的一致时为 `true`）。**只改 Object 名**：mesh 数据、修改器、
GN 组、driver、shape key、归属都不动；同名重复调用幂等。

### 4.7 `blender_outliner_object_delete(scene, objects, confirm_delete)`

**永久删除**指定 Object 数据块（`objects` 是精确名字列表，配合 `confirm_delete=true`
就是授权，schema 里没有虚构的 permission/force 参数）：

* 整批**先校验后删除**，被拒绝的批次**一个对象都不删**：每个对象必须显式属于本场景
  （`object_not_in_scene`）、单场景（`object_shared_across_scenes`）、非链接/覆盖
  （`read_only_object`），且**没有被别的东西引用**（`object_in_use`）：
  * 子对象（`Object.parent`）→ `reason=child_object`；
  * constraint 的 `target`/`object` → `reason=constraint_target`；
  * modifier 的 `object` → `reason=modifier_object`；
  * 几何节点对象插槽/节点上的 `object` → `reason=geometry_nodes_object_reference`；
  * `bpy.data.user_map(subset=...)` 报出的、除直属归属集合以外的使用者 →
    `reason=external_id_user`；
  * `Object.users` 超过直属 Collection 归属数且无法进一步定位 → `reason=users_count`
    （保守兜底：**证明不了就直接拒绝**，`external_user_count` 写上差额）。
  `details.problems` 给出全部问题与 `problems[0]` 对应的错误码；
  `confirm_delete=false` → `confirmation_required`；
* **只删 Object 数据块**：`bpy.data.objects.remove(obj, do_unlink=True)`。Mesh、材质、
  GN 数据全部保留（users 归零也不 purge）；`data_preserved[]` 逐个读回证明数据还在；
* 真正 remove 之后**不可回滚**：中途失败 → `outliner_delete_partial`
  （`deleted` / `residual` / `partial=true` / `not_transactional=true` / `rolled_back=false`
  / `errors[]`），且**只使用删除前缓存的名字**，绝不读已失效的 RNA；
* 返回：`objects` / `deleted` / `objects_deleted` / `residual` / `partial` /
  `data_blocks_deleted`（恒为 `[]`）/ `data_preserved[]` / `references{}` / `ownership{}` /
  `warnings`。

### 4.8 `blender_outliner_collection_display(...)` / 4.9 `blender_outliner_object_display(...)`

在**显式指定的视图层**里设置显示开关（`scene.view_layers` 精确匹配；
`view_layer_not_found` / `ambiguous_view_layer` / `invalid_view_layer`）：

| 参数 | 集合写什么 | 对象写什么 | 作用域 |
| --- | --- | --- | --- |
| `hidden` | `LayerCollection.hide_viewport` | `Object.hide_set(hide, view_layer=...)` / `hide_get` | **单视图层**（小眼睛） |
| `exclude` | `LayerCollection.exclude` | —（对象没有复选框） | **单视图层** |
| `disable_viewport` | `Collection.hide_viewport` | `Object.hide_viewport` | **数据块级**（监视器） |
| `hide_render` | `Collection.hide_render` | `Object.hide_render`（相机同此） | **数据块级**（渲染） |

* 至少给一个开关，全 `null` → `invalid_argument`；非布尔 → `invalid_argument`；
  `true` = 禁用（打开小眼睛/监视器/隐藏渲染），`false` = 恢复；
* **数据块级开关走原 `guard`**：跨场景（`collection_shared_across_scenes`）、多父
  （`collection_multi_parent`）、链接数据（`read_only_collection`）一律拒绝——监视器与渲染
  标志是全局的，不能只改一个视图层了事；**对象**的数据块级开关同样先要求对象显式且**只**属于
  本场景、且未被链接（`object_not_in_scene` / `object_shared_across_scenes` /
  `read_only_object`）；**视图层开关**只要求该集合能在指定视图层里按路径走到
  （`collection_not_in_view_layer`，共享集合也可以只切自己这一层的眼睛/复选框）；
* 对象批处理：整批**先校验**（未知/重名/未入本场景/跨场景/链接/缺 `hide_set`/`hide_get`
  一律先失败，一个字节都不写）；
* 每个开关都经共享 `run_batch`（补偿）写入并**逐开关读回**；Blender 没存住就恢复旧值并报
  `outliner_write_failed`；返回 `requested` / `before` / `after` / `changed` / `idempotent` /
  `view_layer_flags` / `datablock_flags` / `monitor_supported=true` / `warnings`
  （写了数据块级开关会提示“作用于本场景所有视图层”）；
* 对象改名与显示**不修改 data / modifier / GN**（有测试）。

---

## 5. 稳定错误码

| 码 | 含义 |
| --- | --- |
| `invalid_name` | 名字非法，或 Blender 存成了别的名字（已回滚/清理） |
| `collection_name_conflict` / `object_name_conflict` / `data_name_conflict` | 集合名 / 对象名 / **Mesh 数据名**全局撞车：会被改成 `.001`，拒绝（`auto_suffix=false`） |
| `invalid_type` | `type` 不是 `'MESH'` / `'EMPTY'` |
| `invalid_vertices` / `invalid_faces` | 几何校验失败（非有限三元组、`bool`/字符串、面 <3、下标非整数或越界、总量超限） |
| `root_immutable` | 试图重命名/移动/删除场景根集合 |
| `collection_has_children` | 默认删除时目标仍有子集合（需 `recursive=true`） |
| `collection_in_use` | 集合删除前置校验发现外部使用者：`fake_user` / `collection_instance_source` / `geometry_nodes_reference` / `external_users` / `external_parent` |
| `object_in_use` | 对象被引用：`child_object` / `constraint_target` / `modifier_object` / `geometry_nodes_object_reference` / `external_id_user` / `users_count` |
| `confirmation_required` | 删除类命令没有 `confirm_delete=true` |
| `outliner_delete_partial` | 真实 remove 之后失败：`deleted` / `residual` / `partial=true` / `not_transactional=true` / `rolled_back=false` |
| `cycle_hierarchy` | 移动进自己/自己的后代（`reason=self` / `descendant`） |
| `collection_not_in_view_layer` | 按路径走到的 LayerCollection 与解析出的集合不是同一个 |
| `view_layer_not_found` / `ambiguous_view_layer` / `invalid_view_layer` | 视图层名解析 |
| `object_not_in_scene` / `object_shared_across_scenes` / `read_only_object` | 对象写的归属守卫 |
| `collection_tree_too_large` | 子树超过上限（fail closed） |
| （注册期 `RuntimeError`，不是工具错误码） | 拥有者模块没有提供所需助手函数，或没把 helpers 传进来 |

沿用既有码：`invalid_scene` / `scene_not_found` / `ambiguous_scene` / `invalid_path` /
`collection_not_found` / `ambiguous_collection` / `read_only_collection` / `read_only_scene` /
`collection_shared_across_scenes` / `collection_multi_parent` / `collection_outside_scene` /
`invalid_objects` / `object_not_found` / `ambiguous_object` / `duplicate_object` /
`duplicate_path` / `conflicting_paths` / `orphan_object` / `index_truncated` /
`invalid_argument` / `api_unsupported` / `outliner_write_failed`（details 带
`applied`/`compensated`/`residual`/`rolled_back`）。

---

## 6. 上限（响应里的 `limits`，每个遍历与每批几何都有界）

| 项 | 值 |
| --- | --- |
| 路径字节 / 段数 / 单段字节 | 1024 / 64 / 255 |
| 每次调用对象数 | 512 |
| 删除子树集合数 | 512 |
| 每场景视图层数 / LayerCollection 节点数 | 64 / 4000 |
| `vertices` 条数 / `faces` 条数 / 总面角数 | 20000 / 20000 / 120000 |
| 对象行数 / 名字样本数 | 512 / 20 |

命中上限时一律 **fail closed**（`index_truncated` / `collection_tree_too_large` /
`invalid_vertices` / `invalid_faces`），绝不猜测后写入。

---

## 7. MCP 侧（SDK 正式参数）

九个工具都用官方参数 `name` / `title` / `description` / `annotations` / `meta`：

```python
@mcp.tool(
    name="blender_outliner_object_create",
    title="Outliner: create an ordinary Mesh/Empty object",
    description="...",
    annotations=ToolAnnotations(title=..., readOnlyHint=False, destructiveHint=False,
                                idempotentHint=False, openWorldHint=False),
    meta={"domain": "outliner", "bridge_prefix": "outliner_", "mutating": True,
          "idempotent": False, "deletes_data": False,
          "collection_primitives": "blender/outliner.py:register_commands(api)",
          "object_kind": "ordinary_mesh_empty"},
)
```

* 锁定 schema：`object_create(scene: Name, path: Path, name: Name, type: Literal['MESH','EMPTY']
  = 'MESH', vertices: list[list[float]]|None = None, faces: list[list[int]]|None = None)`；
  `vertices`/`faces` 可空、`maxItems=20000`、每个顶点 3 项、每个面 `minItems=3`、
  面下标是**严格 int**（`"type": "integer"`）；
* `readOnlyHint`：九个工具都写状态，因此全部为 `False`；
* `destructiveHint`：重命名/移动/删除为 `True`，创建与显示为 `False`；
* `idempotentHint`：集合创建（同位置复用）、集合改名、集合移动、对象改名、两个显示工具为
  `True`；集合删除 / 对象删除（第二次是“找不到”）与对象创建（同名冲突）为 `False`；
* `deletes_data`：只有两个删除工具为 `True`；
* 两个显示工具额外带 `per_view_layer=true`、**`monitor_property_written=true`**、
  `per_view_layer_flags`（集合 `["hidden","exclude"]`、对象 `["hidden"]`）、
  `datablock_flags=["disable_viewport","hide_render"]`；
* `TOOL_COMMANDS` 显式列出工具名→桥命令映射，另有 `register` 别名，
  因此 `domains._register` 的 `getattr(module, "register")` 约定直接可用。

---

## 8. 本次验证（可复现，全部为内存 fake，未启动 Blender、未写 `Project`）

```powershell
# 1) 本包回归：fake bpy / fake api + MCP schema/转发/审计
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe D:\AI\MCP\Blender\MCP\tests\test_outliner_management.py
# 2) 既有 Outliner 包回归（确认 helper 契约未被影响）
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe D:\AI\MCP\Blender\MCP\tests\test_outliner.py
```

实测结果（本次会话）：

* `tests/test_outliner_management.py`：**87 项通过**（最终集成回归的一部分，退出码 0）。包括所属Scene结构引用与实际弱引用保护的5项新增检查。覆盖：
  * 契约：**恰好 9 个命令**且六个 `outliner_instance_*` 都不存在；helper dict 来自
    `outliner.register_commands(api)` 的**真实返回值**（25 个必需键齐全，helper 不全时注册期
    `RuntimeError` 并点名）；源码不含 `bpy.context.`/`bpy.ops.`/`bpy.types.`/`eval(`/`exec(`；
    两个模块都不碰文件系统/`Project`；桥里没有 `require_instance`/`instance_type`/
    `_transform`，只保留 `instance_collection` 安全扫描；测试进程内 stub `bpy` 不留在
    `sys.modules`；fake 对 Object **与 Mesh** 都建模 `remove` 后 `ReferenceError`。
  * 集合创建/改名/移动/删除：同位置复用、异位置同名拒绝（无 `.001`）、非法名/父守卫、
    被 Blender 改名 → 清理、链接失败 → 只删自建、cycle/根拒绝、补偿与 `compensated` 上报；
    删除的 `confirmation_required` / `collection_has_children` / `recursive` **只删所选子树**、
    会脱离场景的对象先挂场景根、同一对象在子树内多集合时**只规划一次**、对象与 GN 保全、
    实例 source / GN 引用 / fake user / 外部 users 四种 `collection_in_use`、
    跨场景/多父/链接/根拒绝、`remove` 中途失败 → `outliner_delete_partial`
    （`partial=true`、`rolled_back=false`、`residual` 点名、**名字全部来自删除前缓存**）。
  * 对象创建：**两个普通 Mesh 对象**（数据名、归属、顶点/面数、`objects.new_calls`/
    `meshes.new_calls`）、默认空 mesh（GN 就绪）与 `EMPTY`（无 data）、
    类型/几何/名字校验**先于任何分配**（0 new / 0 remove）、名称与 **Mesh 数据名**冲突预检、
    目标集合守卫、被改名 → 清理、链接失败 → 只删自建 mesh+object、`from_pydata` 失败 →
    不留 mesh、既有数据块不变。
  * 对象改名：**data/修改器/GN/driver/shape key/vertex group/constraint 快照前后一致**、
    幂等、撞名拒绝、失败与改名漂移恢复原名、**引用被改 → 恢复原名并报错**、未知/未入场景/
    跨场景/链接拒绝。
  * 对象删除：`confirmation_required`、**只删指定对象并保留 mesh/GN 数据**
    （`data_preserved`、`meshes.remove_calls=0`、数据块仍可读）、坏名/重复名/空列表
    **零副作用**、整批被拒（跨场景共享）时一个都不删、链接/覆盖/未入场景拒绝、
    **引用保护**（子对象 / constraint target / modifier object / GN 对象插槽 / 无法定位的
    `users` 兜底）逐个覆盖并给出 `reason`，解除引用后可以删除、部分删除 →
    `outliner_delete_partial`（`deleted`/`residual`/`errors`，不读死 RNA）。
  * 显示：**集合 4 个开关、对象 3 个开关各自 True/False 往返且互不影响**；小眼睛/复选框是
    视图层级（第二个视图层互不干扰），**监视器与渲染是数据块级**（同时写会体现在所有视图层）；
    共享/多父/链接集合拒绝数据块级开关但允许视图层开关；`view_layer_not_found` /
    `ambiguous_view_layer` / `invalid_view_layer` / `collection_not_in_view_layer`；
    全 null、非布尔、缺 `hide_set/hide_get` 拒绝；每个作用域的**忽略写入 → 读回发现 → 恢复**；
    显示不修改 data/修改器/GN；两个显示 payload 的 JSON 往返。
  * MCP：9 个工具名与 `TOOL_COMMANDS` 一致、`register` 别名、经 `domains._DomainRegistrar`
    代理注册后 9 个名字全部出现、锁定 schema（`Literal` 枚举 + 默认 `'MESH'`、可空
    `vertices`/`faces` 的 20000/3/整数下标）、annotations/meta（`deletes_data`、
    `monitor_property_written=true`、`object_kind`）、9 条转发参数逐一比对、
    15 类错误类型在到达 bridge 之前被 schema 拒绝、`AuditedFastMCP` 写出
    `tool_start`/`tool_end`、工具模块不 import `bpy`。
* `tests/test_outliner.py`：**54 项通过**（包括共享祖先保护；helper dict 契约互换无误）。
* 静态自查：三个文件 `py_compile` 通过；`tests/test_outliner_live_acceptance.py` 本身
  `py_compile` 通过，其 `REQUIRED` 恰好等于既有 6 个命令 + 本包 9 个命令。

**实机状态**：用户已刷新入口，同一Blender加载72桥命令。首次实机27项通过，覆盖创建、
改名、移动、归属/GN引用与显示读回；普通对象删除因所属Scene结构引用误判而被安全拒绝，
已修复并通过266项回归，待再次刷新后完成最终删除验收。下面是需要核对的原生行为；
不能把局部实测写成全部通过，最新结果见 OUTLINER_ACCEPTANCE_STATUS.md：

1. `bpy.data.collections.new` / `objects.new` / `meshes.new` 撞名时的 `.001` 行为
   （本模块先全局预查、再读回，不一致就清理并报错）；
2. `bpy.data.objects.new(name, None)` 产生 `EMPTY`、传入 mesh 产生 `MESH`（写入后读回校验
   `Object.type`，不符即清理并报 `outliner_write_failed`）；
3. `Mesh.from_pydata` 对已校验几何是否直接接受（`edges` 传空列表）以及随后 `Mesh.update()`
   是否稳定；若某个版本拒绝，会走清理路径而不是留下半成品；
4. `Collection.users` 的计数口径与 `bpy.data.user_map(subset=...)` 的返回内容
   （对象删除的引用保护）。两者都是**保守**判断：证明不了就拒绝删除（fail closed），
   不会误删被引用对象；若某个版本把结构性引用也计入，只会更容易拒绝；
5. 删除 Object 后 mesh 数据是否**确实保留**（users 归零但不释放），以及
   `bpy.data.objects.remove(do_unlink=True)` 之后的 `ReferenceError` 行为
   （本模块全部先缓存名字，fake 也按同样方式失效）；
6. `LayerCollection.hide_viewport` / `.exclude`、`Collection.hide_viewport` / `hide_render`、
   `Object.hide_viewport` / `hide_render` / `hide_set(view_layer=...)` /
   `hide_get(view_layer=...)` 的写入与立即读回是否稳定（逐开关读回，不一致就恢复并报错）；
7. 改名对象后 Blender 是否可能改动引用（本模块用引用快照比对，一旦不同就恢复原名并失败）。

---

## 9. 接线与验收状态（2026-10-05）

Codex 已完成源码集成：基础模块返回共享 helper dict；addon 同时注册两个 Outliner
模块；bootstrap 只刷新独立命令模块并复用现有连接；MCP domain 入口注册全部 15 个具名
工具，profile 映射为 all76/core19/outliner34/GN35/algorithm50。

`tests/run_all_tests.ps1` 的无 Blender 单元阶段已接入 Outliner、入口、旧工具和 profile
回归，以及真实 `server.py` stdio profile 检查。最新针对性回归共 266 项通过；正式 SDK 的
五种 profile 清单与禁用工具直接调用检查通过，旧 61 个 schema 保持一致。

**新增命令已加载；对象删除修复尚待再次刷新，最终实机验收待完成。**
`tests/test_outliner_live_acceptance.py` 在 health 缺少命令时，以
`bridge_refresh_required` 拒绝创建任何夹具；刷新后才可在独立、未激活、未保存的临时
Scene 中测试。此脚本不自动加入默认测试运行器，避免改动用户正在工作的实例。
最新证据与剩余工作见 [OUTLINER_ACCEPTANCE_STATUS.md](OUTLINER_ACCEPTANCE_STATUS.md)。

### 已知失败模式（诊断用）

| 现象 | 原因 |
| --- | --- |
| 注册期 `RuntimeError: ... requires the helper key(s) ...` | 拥有者模块没返回 helper dict，或没把 helpers 传进来 |
| `data_name_conflict` | 已有一个 **Mesh 数据块**同名：Object 与 Mesh 命名空间不同，改 mesh 名或换名字即可（本服务不造 `.001`） |
| `object_in_use`（`users_count` / `external_id_user`）而看不出引用者 | 真机里还有未列出的引用（例如节点插槽、driver 目标）：这是**保守拒绝**，请先解除引用 |
| `collection_in_use`（`collection_instance_source`） | 该集合是某个 Collection Instance 的 source；先删实例再删集合 |
| `outliner_delete_partial` | 删除过程被 Blender 中断；`deleted` 是**已经真的删掉**的数据块，无法由本服务恢复（本命令不是事务） |
