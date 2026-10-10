# Outliner 能力盘点与实施边界（2026-10-05）

调查使用当前源码与原生 MCP 实际响应，不仅根据工具名称判断。
初始 health：Blender 5.2.1 LTS、GUI PID 131628、127.0.0.1:9876；57 桥命令。
配置解析与磁盘存在性确认程序为 `D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe`。
Windows 文件资源仅给出版本 5.2，精确补丁版本以 health 的实际运行版本为准。
初始 MCP 源码注册 61 工具，schema 快照保存在 `.runtime/outliner_schema_baseline.json`。

## 既有能力矩阵

| 能力 | 既有状态 | 核对依据 / 缺口 |
| --- | --- | --- |
| 集合列出 | 部分 | scene_summary 只给出全局 Collection 名称，list_scenes 只有每场景集合计数；不能查询完整层级、直属对象及多父关系 |
| 创建集合 | 缺失 | 57 桥命令无 Collection 创建接口；create_case_scene 创建的是场景，不是集合 |
| 指定父集合 / 确保路径 | 缺失 | 无 children 层级操作或幂等路径接口 |
| 创建对象时链接至已有集合 | 已有、受限 | create_mesh_object(collection=名称) 的源码通过 _guarded_collection_target 校验当前场景归属，拒绝跨场景共享；不创建集合，也不能将已有对象移入 |
| 将已有对象链接至集合 | 缺失 | 对象创建接口不满足已有对象链接；不改变该旧接口语义 |
| 移动对象集合归属 | 缺失 | 没有可明确声明来源的批量移动命令 |
| 解除对象链接 | 缺失 | 无 Collection.objects.unlink 命令；移除驱动器/节点等接口不能代替 |
| 集合 viewport/render 显示 | 缺失 | present_object 是视口展示，不能替代集合全局显示标志 |
| 服务端工具域过滤 | 缺失 | build_server 原来注册全部工具，工具名称前缀不产生 tools/list 隐藏或 tools/call 阻止 |

初始实际查询的总工程包含两场景、三对象、三 GN 组；普通 Collection 清单为空。
本任务不会整理这些已有案例，不修改、保存或覆盖总工程文件，也不复现案例。
原有 chairs/models 四资产与已有总工程文件的哈希基线在 `.runtime/outliner_original_hashes.json`。

## API 与设计依据

Blender 5.2 的 [Collection API](https://docs.blender.org/api/5.2/bpy.types.Collection.html)
区分 children（直属子集合）、objects（直属对象）与 all_objects（递归成员）。
[Scene API](https://docs.blender.org/api/5.2/bpy.types.Scene.html) 指定场景有独立根 Collection。
一个 Collection 可被多个父集合或场景引用；对象也可有多个集合归属。
解除链接是删除引用关系，不是删除对象数据块。实现不能假设唯一父节点。
搜索索引取得官方文档内容；网页直读返回 402，因此不把网页直读称为成功。
正式方法的实际可用性还需要新命令加载后的运行验证，单元模拟不能替代实机证据。

SDK 本地源码确认 `mcp==1.30.0`：公开 FastMCP.remove_tool 删除 ToolManager 项，
公开 list_tools 与 call_tool 都使用同一注册表，适合在启动时过滤。
tool decorator 正式支持 title、annotations、meta，用于新增 Outliner 工具的补充分域标识；
这些标识本身不当作隐藏措施。

默认环境未设置 profile 时维持旧配置全部可见，避免静默减少用户现有能力。
新配置显式选择最小 core；任务示例 outliner 为 core + Outliner；算法复现 profile
组合核心、Outliner、GN 与必要对象建模。未知 profile 或未归类的新工具在受限 profile 中拒绝启动。
profile 在不同 MCP 服务实例启动时选择，不声称动态切换工具清单。
验证必须实际比较 tools/list，并直接调用被移除工具确认拒绝且不触达 Blender。

新增命令与临时数据验证的后续实际结果将在开发日志及独立验收记录中追加，
本盘点不预先声明新增命令已经在当前 Blender 加载或实机通过。
