# 按界面浏览工具目录：所有工具常驻

2026-10-05 用户澄清后修正：域只用于分类查看工具说明与参数。**全部93项工具始终注册、出现在 MCP tools/list 中，并可通过 tools/call 调用。** 查看某个界面的目录不会隐藏、禁用或阻止其他界面的工具。

## 使用方式

一次查询某个界面，无需改变会话状态：

```json
{"domains":["geometry_nodes"]}
{"domains":["outliner"]}
{"domains":["geometry_nodes","properties","model_inspection"],"include_parameters":true}
```

以上参数传给 `blender_tool_domains`。返回所选分类的工具名称、说明和完整 inputSchema/outputSchema。省略 domains 返回全部分类的概览；include_parameters=false 可简化为名称与说明。查询不修改任何工具的可用性，也不改变当前目录视图。

如果希望保留当前阅读目录，可使用 `blender_set_tool_domains`：

```json
{"mode":"replace","domains":["geometry_nodes"]}
{"mode":"add","domains":["properties"]}
{"mode":"remove","domains":["geometry_nodes"]}
{"mode":"replace","domains":[]}
```

replace/add/remove 仅替换、追加、移除**目录视图**。旧 enable/disable 参数保留兼容，含义分别为 add/remove，不能启用或禁用工具。空数组恢复通用工具目录；这是25项文档视图，协议中仍有全部93项工具。global 也是可查看的分类，可与其他分类组合。

## 三个目录接口

| 工具 | 输入 | 行为 |
| --- | --- | --- |
| `blender_tool_domains` | domains 可选数组；include_parameters 默认 true | 无 domains 时列分类概览；指定分类时返回其工具定义；不改变阅读状态 |
| `blender_tool_domain_state` | include_parameters 默认 false | 当前阅读分类、工具目录、revision、初始配置及 available_tool_count=93、all_tools_active=true |
| `blender_set_tool_domains` | mode；domains 精确ID数组 | 改当前阅读目录并返回完整定义；不改变注册或调用权限 |

参数最多11个分类ID，重复/未知ID和非数组输入明确报错。失败保留上次目录，幂等操作不增加 revision。分类管理同样写操作审计日志。目录锁只保护阅读状态，不阻挡业务工具调用。

## 界面分类

| ID | 数量 | 用途 |
| --- | --- | --- |
| global | 25 | 工程、场景、诊断、日志、目录接口 |
| outliner | 15 | Collection 与 Object 的层级、成员、创建删除、重命名和显示开关 |
| object_modeling | 3 | 网格、基本体、自定义属性 |
| geometry_nodes | 19 | 节点、接口、修改器、连线、Reroute、布局及编辑器截图 |
| shape_keys | 5 | 形态键查询、创建、修改和删除 |
| drivers | 4 | 驱动器查询、创建、修改和删除 |
| node_frames | 7 | 原生 Frame、布局、Text 注释 |
| node_notes | 6 | 可选注释扩展，内部使用原生 Frame/Text |
| viewport | 2 | 选择、取景和模型预览 |
| properties | 5 | 实例参数读取、写入及迭代 |
| model_inspection | 2 | 求值检查与真实3D视口截图 |

分类建议组合只是阅读提示，不会自动改变当前阅读目录。模型可以在浏览 Outliner 时直接调用 Geometry Nodes、Properties、形态键或驱动器工具；这些调用依然执行各自已有的参数校验、路径边界、工程替换确认与覆盖保护。

## 初始目录配置与旧 profile 兼容

优先级：显式 Python profile > BLENDER_MCP_VIEW_DOMAINS JSON数组 > 旧 BLENDER_MCP_LOCAL_DOMAINS JSON数组 > BLENDER_MCP_PROFILE > 默认。**这些配置现在全部只选择初始文档视图。** 未知/空白 profile、非法分类 JSON 仍在启动时明确报错，防止静默配置错误。

| profile | 初始阅读目录 | 目录工具数 | 常驻可调用工具数 |
| --- | --- | --- | --- |
| global-only / core | 通用工具 | 25 | 93 |
| all | 全分类 | 93 | 93 |
| outliner | outliner | 15 | 93 |
| geometry_nodes | object_modeling + geometry_nodes | 22 | 93 |
| algorithm_reproduction | outliner + object_modeling + geometry_nodes | 37 | 93 |
| algorithm_review | geometry_nodes + properties + model_inspection | 26 | 93 |

global-only 是保留的旧配置名称，现在只表示默认阅读通用工具文档，**不是只开放25项工具**。无需设置 all 才能使用完整能力。

```toml
[mcp_servers.blender.env]
BLENDER_MCP_VIEW_DOMAINS = '["geometry_nodes","properties","model_inspection"]'
```

插件已安装配置中的 BLENDER_MCP_PROFILE=global-only 可以保留；新 server.py 对此配置仍提供全部93项。示例只应登记一个服务，避免与插件重复登记。

## 协议、客户端与验证边界

使用 mcp==1.30.0 正式 low-level handler 对接完整 AuditedFastMCP 注册表。tools/list 永远返回93项；tools/call 直接进入原工具处理，不检查阅读域。初始化 tools.listChanged=false，阅读操作不会发送 notifications/tools/list_changed，也无需客户端刷新或重连。

每个 stdio 客户端会话拥有独立目录视图；实际 Blender 数据仍共享。未分类、缺失或重复注册的工具在初始化前报错，分类不完整不会静默继续。原90项工具定义使用改造前快照严格核对；三项目录接口是新增能力。

**已有旧 MCP 进程需要一次重连以装入修改后的程序。** 这与切换阅读目录不同：新程序加载后，浏览任意分类都无需重连，也无需重启 Blender 或运行 bootstrap。

测试：tests.test_dynamic_domains、tests.test_dynamic_domains_stdio，以及两个 profiles 协议脚本。实测记录见 [工具目录验收](TOOL_DIRECTORY_ACCEPTANCE.md)。[旧动态域验收](DYNAMIC_DOMAINS_ACCEPTANCE.md) 是被本次用户澄清替代的历史记录，不能作为当前可用性约定。

## 完整互斥工具分类表

下表由当前`TOOL_DOMAINS`事实源逐项列出。每项恰好归于一个分类，总93项；global/local 表示分类层次，所有项均常驻可用。运行测试核对实际注册目录与此分类。

| 工具名 | 层 | Domain |
| --- | --- | --- |
| `blender_health` | global | `global` |
| `blender_server_status` | global | `global` |
| `blender_list_commands` | global | `global` |
| `blender_ping` | global | `global` |
| `blender_bridge_probe` | global | `global` |
| `blender_environment` | global | `global` |
| `blender_launch_blender` | global | `global` |
| `blender_reload_review_bridge` | global | `global` |
| `blender_list_scenes` | global | `global` |
| `blender_scene_summary` | global | `global` |
| `blender_list_objects` | global | `global` |
| `blender_object_info` | global | `global` |
| `blender_create_case_scene` | global | `global` |
| `blender_switch_scene` | global | `global` |
| `blender_save_blend` | global | `global` |
| `blender_save_scene` | global | `global` |
| `blender_inspect_blend_file` | global | `global` |
| `blender_open_blend` | global | `global` |
| `blender_operation_log` | global | `global` |
| `blender_log_dashboard` | global | `global` |
| `blender_set_frame` | global | `global` |
| `blender_update_depsgraph` | global | `global` |
| `blender_tool_domains` | global | `global` |
| `blender_tool_domain_state` | global | `global` |
| `blender_set_tool_domains` | global | `global` |
| `blender_outliner_hierarchy` | local | `outliner` |
| `blender_outliner_ensure_path` | local | `outliner` |
| `blender_outliner_link_objects` | local | `outliner` |
| `blender_outliner_move_objects` | local | `outliner` |
| `blender_outliner_unlink_objects` | local | `outliner` |
| `blender_outliner_set_visibility` | local | `outliner` |
| `blender_outliner_collection_create` | local | `outliner` |
| `blender_outliner_collection_rename` | local | `outliner` |
| `blender_outliner_collection_move` | local | `outliner` |
| `blender_outliner_collection_delete` | local | `outliner` |
| `blender_outliner_object_create` | local | `outliner` |
| `blender_outliner_object_rename` | local | `outliner` |
| `blender_outliner_object_delete` | local | `outliner` |
| `blender_outliner_collection_display` | local | `outliner` |
| `blender_outliner_object_display` | local | `outliner` |
| `blender_create_mesh_object` | local | `object_modeling` |
| `blender_create_primitive` | local | `object_modeling` |
| `blender_set_custom_property` | local | `object_modeling` |
| `blender_create_node_group` | local | `geometry_nodes` |
| `blender_create_node` | local | `geometry_nodes` |
| `blender_set_node_property` | local | `geometry_nodes` |
| `blender_set_socket_default` | local | `geometry_nodes` |
| `blender_link_sockets` | local | `geometry_nodes` |
| `blender_unlink_socket` | local | `geometry_nodes` |
| `blender_remove_node` | local | `geometry_nodes` |
| `blender_inspect_node_group` | local | `geometry_nodes` |
| `blender_attach_geometry_nodes_modifier` | local | `geometry_nodes` |
| `blender_set_modifier_input` | local | `geometry_nodes` |
| `blender_list_node_types` | local | `geometry_nodes` |
| `blender_describe_node_type` | local | `geometry_nodes` |
| `blender_batch_graph_edit` | local | `geometry_nodes` |
| `blender_geometry_nodes_layout` | local | `geometry_nodes` |
| `blender_geometry_nodes_move_nodes` | local | `geometry_nodes` |
| `blender_geometry_nodes_reroute_insert` | local | `geometry_nodes` |
| `blender_geometry_nodes_reroute_move` | local | `geometry_nodes` |
| `blender_geometry_nodes_reroute_delete` | local | `geometry_nodes` |
| `blender_geometry_nodes_capture_editor` | local | `geometry_nodes` |
| `blender_inspect_shape_keys` | local | `shape_keys` |
| `blender_create_shape_key` | local | `shape_keys` |
| `blender_set_shape_key_coordinates` | local | `shape_keys` |
| `blender_set_shape_key` | local | `shape_keys` |
| `blender_remove_shape_key` | local | `shape_keys` |
| `blender_inspect_drivers` | local | `drivers` |
| `blender_add_driver` | local | `drivers` |
| `blender_update_driver` | local | `drivers` |
| `blender_remove_driver` | local | `drivers` |
| `blender_node_frame_create` | local | `node_frames` |
| `blender_node_frame_get` | local | `node_frames` |
| `blender_node_frame_parent` | local | `node_frames` |
| `blender_node_frame_unparent` | local | `node_frames` |
| `blender_node_frame_update` | local | `node_frames` |
| `blender_node_frame_set_text` | local | `node_frames` |
| `blender_node_frame_layout` | local | `node_frames` |
| `blender_node_note_status` | local | `node_notes` |
| `blender_node_note_extension` | local | `node_notes` |
| `blender_node_note_create` | local | `node_notes` |
| `blender_node_note_get` | local | `node_notes` |
| `blender_node_note_update` | local | `node_notes` |
| `blender_node_note_delete` | local | `node_notes` |
| `blender_capture_preview` | local | `viewport` |
| `blender_present_object` | local | `viewport` |
| `blender_properties_list_parameters` | local | `properties` |
| `blender_properties_get_parameter` | local | `properties` |
| `blender_properties_set_parameter` | local | `properties` |
| `blender_properties_set_parameters` | local | `properties` |
| `blender_properties_iterate_parameters` | local | `properties` |
| `blender_model_inspection_inspect` | local | `model_inspection` |
| `blender_model_inspection_capture_viewport` | local | `model_inspection` |
