# 几何节点里的数据块引用（datablock references）

本文说明本 MCP 如何把 JSON 里的"引用"写进 Blender 的指针属性。所有结论都在本机
**Blender 5.2.1 LTS** 上用 `tests\blender_review_regressions.py` 实测得到，不依赖猜测。

复现命令：

```powershell
D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe --background --factory-startup --python-exit-code 1 `
    --python D:\AI\MCP\Blender\MCP\tests\blender_review_regressions.py
```

报告：`MCP\tests\reports\review_report.json`。

---

## 1. 引用形式

JSON 里用**结构化对象**表示数据块，绝不用字符串路径、也绝不 `eval`/`exec`：

```json
{"id_type": "OBJECT", "id_name": "Cube.001"}
```

| `id_type` | 解析来源（显式集合） | 对应 RNA 类 |
| --- | --- | --- |
| `OBJECT` | `bpy.data.objects` | `Object` |
| `NODETREE` | `bpy.data.node_groups` | `NodeTree` |
| `MATERIAL` | `bpy.data.materials` | `Material` |
| `COLLECTION` | `bpy.data.collections` | `Collection` |
| `IMAGE` | `bpy.data.images` | `Image` |

- `id_name` 缺失、为 `null` 或空串 → **清空指针**（写 `None`）。整条参数直接传 `null` 同样表示清空。
- 其它 `id_type`（例如 `SCENE`）会得到 `invalid_reference`，并返回受支持的种类列表。
- 名字不存在 → `reference_not_found`（附带该集合中可用名称的前 50 个）。
- 类型不符 → `reference_type_mismatch`（附 `expected_rna_type` 与 `given_id_type`）。
- **失败时旧指针保持原样**：解析与类型校验都在 `setattr` 之前完成。

读回时指针一律序列化成同一种结构（外加 `rna_type`），例如
`{"id_type": "OBJECT", "id_name": "Cube", "rna_type": "Object"}`；空指针读回 `null`。

## 2. 三类可用位置（均已实测）

### 2.1 节点指针属性：`GeometryNodeGroup.node_tree`

```json
{"op": "set_node_property", "node": "Inner", "property": "node_tree",
 "value": {"id_type": "NODETREE", "id_name": "RevInner"}}
```

- RNA `fixed_type` 是 `NodeTree`。**Blender 会欣然接受一个 `ShaderNodeTree`** 并生成一个坏节点，
  因此本服务额外要求 `GeometryNodeGroup` 必须是 `GeometryNodeTree`，否则报 `reference_type_mismatch`。
- `null` 可清空。重新赋同一个组是**幂等**的：Blender 不会重建插槽，链接保持不变。
- **换成另一个组会重建该节点的插槽，Blender 会丢掉该节点上的链接**（同 identifier 时插槽默认值也可能被沿用）。
  响应因此总是给出 `links_before` / `links_after`，并在链接变少时给出 `warnings`；
  重连之后再求值即恢复正常（实测：换成"输出单位立方体"的组 → 求值 8 顶点；换回 → 16 顶点、Z=2）。
- **工作顺序**：先设 `node_tree`，再连插槽（`set_node_property` 之后 `Inner` 节点才有 `Geometry` 插槽）。

### 2.2 插槽默认值（含组接口默认值）

- Object Info 的 `Object`、Set Material 的 `Material`、Collection Info 的 `Collection`、
  Image Texture 的 `Image` 等 DATABLOCK 插槽：`set_socket_default` 接受引用或 `null`。
- 组接口的指针插槽（`create_node_group` 的 `interface[].default_value`）同样接受引用或 `null`。
- 实测：`Object Info.Object` 引用一个 5 顶点对象后，挂该组的对象求值得到 **5 个顶点**；
  传 `null` 后求值为 **0**。
- 非指针插槽收到引用 → `invalid_reference`；指针插槽收到数字/字符串 → `invalid_reference`。

### 2.3 已暴露的 GN 修改器参数（5.2 支持）

- 5.2 把每个实例的参数存在 IDProperty 里：`modifier.properties.inputs[<identifier>]["value"]`。
- 指针参数（`NodeSocketObject/Material/Collection/Image`）**可以**写入数据块，`null` 可清空；
  本服务把"引用 → 数据块"解析好再写，并按接口插槽的 RNA 固定类型校验。
- **重要**：IDProperty 不校验类型，直接写字符串也会"成功"但不再指向任何数据块；
  因此本服务对指针参数**只接受引用对象或 `null`**，其它值报 `invalid_modifier_input`
  （details 内含 `invalid_reference`）。实测切换到另一个对象的引用会立刻改变求值几何（5 → 4 顶点）。
- 写入后必须给对象打 tag 才会重算（`object.update_tag()` + 依赖图更新），实现里已内建。
- `NodeSocketGeometry` 参数仍然**不能**按值设置（本版本不支持，返回明确错误）。

## 3. 明确不支持的部分

| 情况 | 结果 |
| --- | --- |
| `id_type` 为 `SCENE`/其它未列出的种类 | `invalid_reference`（给出支持列表） |
| 指针位置传字符串/数字 | `invalid_reference`（避免"看起来成功"的坏引用） |
| `GeometryNodeGroup` 指向 `ShaderNodeTree` | `reference_type_mismatch` |
| 按名字猜路径、或任何 `eval`/`exec` 式解析 | 不存在：只走上述五个 `bpy.data` 集合 |
| 克隆/复制被引用的组 | 不会发生：引用即复用已存在的数据块（实测 node group 数量不变） |

## 4. 组指针变更后的插槽默认值

实测：把 `Inner` 从 `RevInner`（`Lift` 默认 2.0）换到另一个接口 identifier 相同的组时，
**该节点上已有的插槽默认值会被沿用**（Z 仍为 2.0，而不是新组的 5.0）。
换组之后请把你在意的取值显式再设一遍（`set_socket_default` / `set_modifier_input`），
或者换用结构不同的组（不同输出）来确认切换已生效。

## 5. 回归覆盖

`tests\blender_review_regressions.py` 中与本文相关的检查（全部通过，共 41 项）：

- `group_node_pointer_starts_null` / `group_node_pointer_reference_readback`
- `group_pointer_wrong_type_refused_and_unchanged` / `group_pointer_rejects_shader_tree`
- `group_pointer_missing_reference_refused` / `group_pointer_plain_string_refused`
- `pointer_reassignment_same_tree_reports_truthfully` / `pointer_swap_to_other_group_is_reported_truthfully`
- `group_pointer_clear_and_reuse_without_clone` / `pointer_swap_back_to_original_group`
- `nested_group_sockets_exist_after_pointer_set` / `nested_group_reuse_evaluates_geometry`
- `nested_pointer_swap_changes_evaluated_geometry` / `unlink_socket_removes_exactly_one_link_and_changes_geometry`
- `interface_pointer_default_reference_readback` / `socket_pointer_default_reference_readback`
- `socket_pointer_clear_with_null` / `modifier_pointer_parameter_reference`
- `modifier_pointer_parameter_switch_changes_geometry` / `modifier_pointer_parameter_clear_with_null`
- `modifier_pointer_parameter_rejects_plain_string` / `modifier_pointer_errors_leave_state_unchanged`
