# 参数化模型检查与迭代能力盘点

2026-10-05，调查当前源代码、正式 SDK、原生 MCP 响应；不根据工具名称推断功能。
目标是增强 MCP，不重做算法案例。源码/Git/文档/日志在 MCP；验收模型放在独有
Project 子目录，不覆盖已完成总工程。

## 初始运行状态

- 当前 Blender PID127096，5.2.1 LTS、GUI、127.0.0.1:9876。
- 磁盘目标 Version/5_2_1_LTS/blender.exe；精确补丁版本以运行 health 为准。
- 文件 Project/算法复现/算法案例总工程.blend；当前Scene Alg00_01_两数相加。
- 正式 server.py 工具清单为76，bridge为72；Outliner15命令已加载。
- 36对象、31个被Modifier引用的主GN组已做只读快照；文件即时SHA256在
  `.runtime/algorithm_review_asset_baseline.json`，原76 schema在
  `.runtime/algorithm_review_baseline_tools.json`。快照收集仅调用具名 MCP。

## 实际能力与缺口

| 能力 | 初始状态 | 实际依据及最小补充 |
| --- | --- | --- |
| 修改器当前实例值 | 部分支持 | object_info返回Modifier名称、节点组、identifier/name/value；Alg00_02实际有Socket_0–3四输入。缺少完整类型、范围、隐藏、字段模式和可写性报告 |
| 单参数写入 | 已支持基础行为 | set_modifier_input已使用5.2 modifier.properties.inputs[identifier]["value"]；旧实现兼容显示名且宽松转换，保留旧调用，新增严格identifier入口 |
| 实例隔离/批量输入安全 | 部分支持 | 旧setter只作用指定实例，但批量内部循环会部分写入且类型/长度/field未完整预检；补整批预检、快照恢复和读回 |
| 求值后Mesh概要 | 已支持部分 | object_info/evaluated_mesh_stats读to_mesh后finally清理，输出counts/world bounds；当前依赖图来自activeScene，需精确场景及可用组件/警告报告 |
| 模型图像回传 | 已支持预览 | capture_preview已有native MCP image与路径保护；允许视口渲染/相机回退，不能替代要求的真实UI区域截图；复用图片/路径安全 |
| GN节点结构/Frame | 部分支持 | inspect_node_group已有socket、links、位置、parent；Frame布局独立原生工具已存在。不足：精确树路径、draw依赖尺寸、multi-input排序与Reroute语义图 |
| GN编辑器真实截图 | 缺失 | 72命令中无编辑器区域截图；新增明确目标区域、树、fit/subset、sidebar与MCP image返回 |
| 原生Reroute单点创建 | 可用底层原语 | create_node(NodeReroute)+link_sockets可手工操作；不能保证有界插入、分支、多输入顺序或失败回滚，新增具名事务工具 |
| 域/profile真实过滤 | 已支持 | FastMCP.remove_tool控制同一ToolManager的list与call；沿用原机制，新增properties/model_inspection与algorithm_review显式任务子集 |

## API依据与未确认点

官方5.2文档索引确认 [NodesModifier](https://docs.blender.org/api/5.2/bpy.types.NodesModifier.html)
有properties、is_input_visible、is_input_used和node_warnings；字段记录键仍需当前RNA实测。
[NodeLink](https://docs.blender.org/api/5.2/bpy.types.NodeLink.html) 的multi_input_sort_id只读，
最高ID在顶部，正式swap_multi_input_sort_id只能用于同一multi-input socket的两条边。
[Node](https://docs.blender.org/api/5.2/bpy.types.Node.html) 的location相对Frame，dimensions在
绘制后才有真实尺寸；不能把未画尺寸当可靠布局依据。
[Window](https://docs.blender.org/api/5.2/bpy.types.Window.html) 提供screenshot(region=矩形)，
返回底到顶RGBA memoryview，官方示例通过imbuf写PNG；可用于真正编辑器/视口区域捕获。

上述引用来自官方文档的搜索索引。直接打开部分版本页面返回402，未将失败直读称为成功。
截图的目标UI重绘、字段模式、原生Reroute排序和求值结果必须实机确认；此盘点不预报通过。
