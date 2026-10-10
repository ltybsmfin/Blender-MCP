# 参数化模型检查与迭代

## 按界面查看工具

全部93项工具始终可用。调用
`blender_tool_domains(domains=["geometry_nodes","properties","model_inspection"])`
查看参数化检查相关的26项工具文档和参数；无需开启工具、刷新清单或重连。
`BLENDER_MCP_PROFILE=algorithm_review` 只选择这个初始阅读目录。
Outliner、形态键、驱动器和其他分类的工具可以随时直接调用。

目录查询不改变 Blender 模块注册或工程数据，也不替代工程替换确认、覆盖保护。
完整目录语义见 [工具目录说明](TOOL_PROFILES.md)。

## 新增具名工具

以下名称均带 `blender_` 前缀，表中省略该前缀。

| 工具 | 主要参数与行为 |
|---|---|
| `properties_list_parameters` | `object, modifier`；返回接口 identifier、名称、类型、共享默认、当前实例值、范围、单位、隐藏、连接、Field/属性模式、支持写入与拒绝原因 |
| `properties_get_parameter` | `object, modifier, identifier`；按稳定 identifier 读取一个输入 |
| `properties_set_parameter` | `object, modifier, identifier, value, mode, attribute_name`；单值或明确属性模式，校验类型/范围/数据块引用 |
| `properties_set_parameters` | `object, modifier, changes`；最多 128 项，整批预检，写入或读回失败恢复输入快照 |
| `properties_iterate_parameters` | 上述批量参数，加 `scene, window_index, area_index, fit, relative_path, overwrite`；返回参数 diff、变更前后求值摘要、真实视口 MCP image |
| `model_inspection_inspect` | `object, scene, modifier`；按目标场景/ViewLayer 的依赖图读取完整修改器栈结果，不应用修改器 |
| `model_inspection_capture_viewport` | `object, scene, window_index, area_index, fit, relative_path, overwrite`；返回实际 3D Viewport 区域像素与模型摘要 |
| `geometry_nodes_layout` | `group, object, modifier`；限定 GeometryNodeTree，返回节点、父 Frame、坐标、缓存尺寸、socket、links、多输入顺序、Reroute |
| `geometry_nodes_move_nodes` | `group, positions=[{node,x,y}], absolute=false`；只移动明确命名的节点，可移动 Frame；不自动整理整图 |
| `geometry_nodes_reroute_insert` | `group, links=[{from_node,from_socket,to_node,to_socket,multi_input_sort_id?}], points, shared_branch`；在已有精确 links 上插入原生 Reroute |
| `geometry_nodes_reroute_move` | `group, positions, absolute`；仅移动明确命名的 Reroute |
| `geometry_nodes_reroute_delete` | `group, nodes`；重连上游至全部下游，保持多输入顺序，失败回滚 |
| `geometry_nodes_capture_editor` | `group, object, modifier, window_index, area_index, fit=KEEP/ALL/SELECTED, nodes, include_sidebar, relative_path, overwrite`；真实节点编辑器区域截图与布局 |

读取旧节点类型、描述 socket、完整节点树的已有工具继续复用。参数写入只修改 `modifier.properties.inputs[identifier]` 的实例输入；不修改共享接口默认值。多个修改器存在时需明确名称，重复显示名不作为 identifier 使用。

属性模式依据 `getattr(modifier.properties.inputs, identifier).bl_rna.properties['type'].enum_items` 的现场枚举及现有 `type` 存储键判定；`attribute_name` 即使非空也不能证明当前处于 ATTRIBUTE。切换 ATTRIBUTE 同时写属性名和对应枚举值，VALUE 显式恢复单值模式，整批读回验证模式与数值。枚举数字不硬编码。返回 `structure_type`、`field_capable_socket_type`、`supports_attribute_mode`、当前模式和运行时显隐/使用状态；`is_field` 仅表示已证明的 ATTRIBUTE 模式，未知状态为 null。动态隐藏输入同样拒绝写入。

无法证明的菜单枚举、隐藏/输出/只读/驱动输入和不支持的类型明确拒绝。Object/Collection/Material 等引用必须传结构化数据块名称，不能传任意 Python。参数修改后返回实际对象标记、求值和警告；不要将写入成功自动理解为每个几何组件都可读取。

菜单枚举优先读取该实例生成的 RNA `value` 属性，返回每项 `identifier/name/value`。整数是 Blender 的真实枚举值，不是菜单第几项；例如实际值 10、42 不会被错误转换为 0、1。传 identifier 时按现场映射写入整数。整数存储却无法获得可靠数值映射时拒绝写入。

## 图像与检查证据

Blender 5.2.1 使用 `Window.screenshot` 从指定窗口的编辑器矩形读取真实 framebuffer，以 `imbuf` 写入 PNG；图像缓冲区通过 `ImBuf.free()` 释放。仅 API 缺失时才使用真实 `screen.screenshot_area`，从不使用渲染替代编辑器截图。

截图按 prepare → 等待目标区域实际绘制 → commit → 恢复 UI 的具名 bridge 命令编排。绘制探针核对窗口/区域指针、真实编辑树与场景；拒绝错误树、未绘制、空图和失败捕获。PNG 只发布至 `Project/previews`，并通过 MCP `ImageContent` 回传，包含路径、尺寸、哈希、目标、视图和恢复状态。磁盘路径不等同于 AI 已看到图像；验收还需要读取返回的 image 内容。

临时 UI 配置与选择会恢复，所有恢复失败如实报告。节点 `dimensions` 是最近一次绘制的缓存值；独立布局查询不能证明它刚刚绘制过，截图后的布局才有对应绘制证据。`fit=KEEP` 是编辑器截图默认模式；显式 fit 会调整画布视图，节点坐标与连线不受影响。Node Editor 的 View2D 平移/缩放不能承诺精确恢复，避免对用户原有画布使用显式 fit。

省略区域索引时优先选择最大的合适区域，显式索引保持精确。新编辑器及 sidebar 先绘制再取景；截图等待平滑导航结束后的目标绘制，不更改动画偏好。普通非 asset 的 GN 组通过原生 `space.path.start(tree)` 定位，不为截图修改组的 Modifier/Tool Usage。

模型计数与包围盒来自求值后几何，临时 mesh 均清理。Python evaluated Object.data 无法可靠暴露内部 GeometrySet 的全部曲线/点云/体积；这些不可读组件列为 unknown，空 mesh 不能证明模型没有可见几何。实例摘要仅统计依赖图实际暴露的实例。`modifier` 选择报告对象，不会截断或修改栈。

复合迭代流程中，参数批量写入自身是原子的。如果后续检查或截图失败，工具返回错误并明确 `parameters_committed=true` 和 diff，避免把已成功写入的参数伪报为回滚。

## Reroute 语义

单条 link 使用独立 Reroute；同源一对多可共用分支；不同源进入同一 multi-input 始终保持独立转接点。排序使用 Blender 5.2 的 `multi_input_sort_id` 与 `swap_multi_input_sort_id`。插入/删除与排序重建保留静音连线，数据流等价检查也比较有效静音状态。操作先保存拓扑、位置及 Reroute 标签/显示状态；失败恢复同一节点树，读回确认多输入顺序、节点和连线。恢复未完成时明确返回 `rolled_back=false`，不把部分恢复描述为成功。不替换主 GN 数据块。Reroute 是曲线锚点，不能保证连线全部横平竖直。

## 验证入口与证据分层

- 离线回归：Properties、Geometry Nodes 编辑、截图、模型检查、bootstrap、profiles 各测试模块；fake RNA 不冒充 Blender 实测。
- 正式服务协议：`tests/test_algorithm_review_profiles.py`，所有profile均93项工具；旧76项input/output schema不变。目录协议测试还核对原90项完整定义、目录查询及跨目录调用。
- 实机：`tests/test_algorithm_review_live.py`；所有数据读写走具名 MCP，独立 Scene/GN/对象导出到唯一 `Project/MCP验收/MCPReview_*/fixture.blend`。`--resume` 续验上一次夹具，避免截图重试创建更多夹具。日志在 `.runtime/algorithm_review_live.json` 与审计目录。
- 扩展实机：`tests/test_algorithm_review_extended_live.py`；复用 Scene，验证真正 Field/菜单/多种值和引用、正实例、Frame/Reroute 图片。首次创建小型验收图，重试复用；独立导出 `extended_fixture_instances.blend`，不覆盖现有工程。

当前验收进度和未验证事项见 `ALGORITHM_REVIEW_ACCEPTANCE_STATUS.md`。不覆盖算法案例总工程，不把测试报告中的 `ok=false` 描述为已验收。

## 官方 API

- [Window.screenshot](https://docs.blender.org/api/5.2/bpy.types.Window.html)
- [ImBuf.free](https://docs.blender.org/api/5.2/imbuf.types.html)
- [Context.evaluated_depsgraph_get](https://docs.blender.org/api/5.2/bpy.types.Context.html)
- [NodeLink 多输入排序](https://docs.blender.org/api/5.2/bpy.types.NodeLink.html)
- [Blender v5.2.1 动态输入 RNA 与数据路径源码](https://raw.githubusercontent.com/blender/blender/v5.2.1/source/blender/nodes/intern/geometry_nodes_srna.cc)
- [Blender v5.2.1 各 socket 的 type / attribute_name 定义](https://raw.githubusercontent.com/blender/blender/v5.2.1/source/blender/nodes/intern/node_socket.cc)
- [Blender v5.2.1 NodeLink 静音与排序定义](https://raw.githubusercontent.com/blender/blender/v5.2.1/source/blender/makesrna/intern/rna_nodetree.cc)
- [Blender v5.2.1 Node Editor 类型校验和 path API 定义](https://raw.githubusercontent.com/blender/blender/v5.2.1/source/blender/makesrna/intern/rna_space.cc)
- [Blender v5.2.1 ALL/SELECTED 取景和绘制边界](https://raw.githubusercontent.com/blender/blender/v5.2.1/source/blender/editors/space_node/node_view.cc)
- [PreferencesView.smooth_view](https://docs.blender.org/api/5.2/bpy.types.PreferencesView.html)

## 固定模块刷新维护

`blender_reload_review_bridge()`（无参数）常驻global。仅重新载入固定的
review_capture_common、properties、geometry_nodes_edit、model_inspection、geometry_nodes_capture。
核对模块来源均为 MCP/blender；不接受代码、路径或模块名。保留 listener、token、PID、工程路径和场景，不保存工程。
导入失败恢复命令注册表，明确报告模块 globals 不保证整体回滚；不伪报成功。

这个具名入口必须先在当前 Blender 通过磁盘 bootstrap 安装一次。之后固定检查模块的源码修复可通过 MCP 刷新，无需反复手动执行 bootstrap。
入口自身、addon 主体或新增注册机制的变更仍需要 bootstrap；它不是任意代码执行工具。
维护入口在所有初始profile和局部选择中常驻；仍须满足其原有桥接在线及固定来源检查。

2026-10-05 已在实际 PID151944 安装并验证固定刷新入口。Blender 已关闭时可调用 `blender_launch_blender` 自动启动并加载 bootstrap；已加载固定模块的修复不需要反复手动运行脚本。
