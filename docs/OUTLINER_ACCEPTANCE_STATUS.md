# Outliner 接入验收状态

更新：2026-10-05。源码集成和 MCP 清单验收已完成，当前 Blender 的新增命令实机验收尚待完成。

## 已取得的证据

- 初始能力矩阵、实际版本和 Collection 数据模型：[OUTLINER_CAPABILITY_AUDIT.md](OUTLINER_CAPABILITY_AUDIT.md)。现有接口只能部分列出集合、在创建对象时指定已有集合，缺少层级和成员编辑。
- 原 61 个 MCP 工具的 inputSchema/outputSchema 快照：`.runtime/outliner_schema_baseline.json`。
- 真实 `server.py` 的五个独立 stdio 服务进程：`.runtime/outliner_profiles_integration/report.json`。使用官方 ClientSession 进行 tools/list、tools/call；项目现有标准管道适配器只负责 Windows 管道传输，不改协议或服务端。
- 最终全部 15 个 Outliner 工具已接入，真实服务清单实测：all=76、core=19、outliner=34、geometry_nodes=35、algorithm_reproduction=50。Outliner 清单无 GN/3D View/截图/形态键/驱动器/Frame/注释；被排除工具直接调用返回 Unknown tool。原 61 个 inputSchema/outputSchema 与基线一致。未知 profile 明确退出 2，无 traceback 或协议 stdout 污染。
- 针对性回归：`python -m unittest tests.test_outliner tests.test_outliner_management tests.test_file_bootstrap tests.test_file_open tests.test_node_frames tests.test_connection_startup tests.test_tool_profiles`，最新266项通过。包括5项新增所属Scene/实际弱引用与完整归属防回归。这些是模拟或 SDK 检查，不是 Blender 运行验证。
- 正式 SDK 清单命令：`python tests/test_outliner_profiles_integration.py`，通过；无夹具工具或 stub，不连接 Blender。测试已接入 `tests/run_all_tests.ps1` 无 Blender 单元阶段；实机脚本不默认自动运行。
- 入口保留现有监听、timer、token 和当前 Scene；只刷新独立模块，不重注册现有 UI 类，不在 Blender 主线程阻塞探测自身。

## 用户补充的范围

2026-10-05 用户要求 Collection/对象的创建、删除、移动、重命名和显示开关。
截图澄清“实例”指普通场景 Object，其中截图对象为 Mesh Object；不把 Collection Instance 当作这次需求。
集合与对象的小眼睛隐藏、全局禁用视口、禁止渲染分开；集合还支持视图层排除。已实现 6 个基础工具、4 个集合生命周期工具、3 个普通对象生命周期工具和 2 个显示工具。对象移动复用成员关系工具，不移动其三维位置。集合删除保留模型数据，对象删除须明确确认、保护引用；永久删除失败诚实返回已应用/残留情况，不宣称事务。

## 待完成

- 用户已运行bootstrap，health实际确认同一PID131628、5.2.1LTS、72桥命令，全部15个Outliner命令已加载。当前Scene由用户切换为Alg00_坐标重映射与点构造，测试没有切换它。
- 首次真正进入实机验收已有27项检查通过：层级/幂等、批量link/move/unlink、GN节点与非空连线保持、无效请求零部分效果、集合4/对象3显示开关True/False读回、对象改名保留底层数据。失败在普通对象删除：user_map把所属Scene也列作ID引用者，旧保护误判；报告保留 .runtime/outliner_live_after_refresh_failed.json，不把此轮写成全通过。
- 已修复：只在完整归属证明所属Scene、Object.users等于直属Collection数量、无摄像机/marker/driver/keying-set实际引用时排除该结构性Scene ID；其它引用与未知状态仍拒绝。另修补删除整批校验对截断归属报告的依赖。5项新增回归及266项整体检查通过。
- 已请求再运行同一bootstrap加载修复，等待结果。入口不关闭Blender、不重注册UI、不保存或替换工程。普通对象删除与递归集合删除的最终实机验收尚未完成。
- 新命令加载后，仅通过具名 MCP 工具在独立临时数据上验证层级、幂等、对象归属、GN 引用、删除和显示开关。不开启算法复现，不改写或保存用户工程文件。
- `tests/test_outliner_live_acceptance.py` 先检查所有命令，缺失时拒绝创建任何夹具；刷新后才创建未激活、未保存的临时 Scene。显示开关逐项 True/False 读回，错误后比较完整夹具层级；报告 `.runtime/outliner_live_acceptance.json`。脚本留下内存中的临时 Scene/核验数据，不自动保存。
- chairs/models 的四个原资产哈希仍与初始基线一致。算法总工程在本任务期间发生了外部更新；本任务未调用其保存或编辑操作，不能把该文件称为相对初始基线未变。实机验证需要再次取即时基线，并与外部活动区分。

最终结果记录本文件及 DEVELOPMENT_LOG.md；所有文件位于 MCP 内，无提交或推送。

## 按目标逐项复核

| 目标要求 | 当前证据与结论 |
| --- | --- |
| 1：现有能力实际调查、版本/路径/配置 | 已完成，原生 health/list_commands/list_scenes 与源码依据记录在 OUTLINER_CAPABILITY_AUDIT.md；运行版本 5.2.1 LTS、PID131628、9876 |
| 1：Blender DAG、根集合、多集合归属和 unlink | 源码与官方 API 依据已核对；模拟覆盖 DAG/环/共享祖先；原生新增接口运行行为待刷新后确认 |
| 2：操作域独立入口和正式 SDK 过滤 | domains.py、tool_profiles.py 已接线；真实 server.py 的 tools/list、被排除工具 tools/call 均验证 |
| 2：显式 profile、最小新配置、兼容默认、无动态承诺 | 四个 examples/profiles TOML 显式配置；旧环境默认 all 保留；TOOL_PROFILES.md 记录重连流程与失效配置拒绝 |
| 2：具名小 schema、annotations/meta、无万能代码工具 | 两个类型化 MCP 模块共15工具；字段、转发、标识与审计单元覆盖 |
| 3/4：六个基础工具、路径、幂等、整批验证、显式来源 | outliner.py 和独立 MCP 模块已实现；54基础单元通过；真实临时对象/层级验收待完成 |
| 用户补充：集合/普通对象生命周期与显示开关 | management 两模块已实现9工具，87项单元通过；创建/改名/移动与显示已实机验证；普通对象删除误判已修复，待加载后完成最终验收 |
| 4/6：不隐式改名、移动3D位置、保存或修改已有模型 | Outliner 源码只作用于明确集合/成员/名称/显示；没有文件保存或任意执行接口；实机前后比较与日志核验待完成 |
| 5：总集合→章节→案例、多对象、GN 全局语义 | README/OUTLINER.md/API 已支持；算法复现总提示词已同步；未开始算法案例复现 |
| 6：独立注册、不依赖可选注释、旧接口兼容 | addon/bootstrap/domain 已接线；正式服务旧61个 input/output schema 与基线一致；新模块不导入注释插件 |
| 6：稳定错误、读写/审计边界和失败报告 | 基础/管理单元覆盖错误零副作用、补偿与真实不可回滚限制；MCP不导入bpy、写操作经既有bridge；实机错误验收待完成 |
| 7.1–7.5：临时层级、幂等、归属/GN引用、unlink与错误无副作用 | tests/test_outliner_live_acceptance.py 已准备并强化非空GN连线比较；尚未进入实机写操作，不能标记通过 |
| 7.6：health/清单加载和旧工具可用 | 旧工具原生可用；health确认72命令，其中15个Outliner全部已加载；删除判断修复须再次刷新 |
| 7.7：不同 profile 清单与拒绝，重连流程 | 正式SDK五个独立进程实测76/19/34/35/50、禁用工具拒绝，通过；配置示例与重连说明齐全 |
| 7.8：README/API/profile限制及经验提示词 | 已更新，文档区分静态、协议与Blender实机状态 |
| 项目隔离、原资产和Git约束 | MCP相关文件留在MCP；无提交/推送；四模型最新哈希匹配 .runtime/outliner_hashes_recheck.json；算法总工程与初始基线不同，不宣称未变 |

此前运行门禁，实际退出码2，报告含 missing_commands、PID/版本、checks=[]、
fixture_created=false、saved_blend=false；前后 list_scenes 保持两场景/六对象，未创建夹具。
这证明入口未刷新时测试停止，**不证明任何新增 Blender 操作已通过**。
