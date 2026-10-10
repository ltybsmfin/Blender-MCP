# 参数化检查增强：验收状态

> 目录语义更新（2026-10-05）：本文实机建模/截图证据属于当时验收；profile工具数和禁用域检查为历史方案。
> 当前全部93项常驻，域只查看工具说明。现行协议验收见 [TOOL_DIRECTORY_ACCEPTANCE.md](TOOL_DIRECTORY_ACCEPTANCE.md)。

## 当前结果（2026-10-05，已完成本次功能验收）

实际程序为 `Version/5_2_1_LTS/blender.exe`，health 确认 **5.2.1 LTS、GUI、PID151944、127.0.0.1:9876**。
旧 Blender 已关闭；通过具名 `blender_launch_blender` 启动、自动加载 bootstrap，再通过具名 `blender_open_blend` 打开既有专用夹具。
维护命令现已实际安装，多次 `blender_reload_review_bridge()` 刷新保留 PID、工程路径、Scene 和连接，不再等待用户手动运行脚本。

当前会话只加载 `Project/MCP验收/MCPReview_f4ddb108/fixture.blend`。
扩展测试图保存到同目录 `extended_fixture_instances.blend`，使用 `save_scene(overwrite=false)`；没有覆盖原夹具或算法案例总工程。
全部建图、参数、节点整理、截图和保存测试夹具的操作均通过具名 MCP；无桌面自动化、任意 Python 工具或 raw socket 绕过。

| 要求 | 当前验收证据 |
| --- | --- |
| 1 能力调查、架构、SDK/API | 初始76工具/72命令能力矩阵；实际 mcp1.30.0、pydantic2.13.5；v5.2.1 官方 RNA/socket/Node Editor 源码与真实运行行为核对 |
| 2 独立域与既有 profile 兼容 | 正式 server.py stdio：all90、algorithm_review26、core20、outliner35、geometry_nodes42、algorithm_reproduction57；禁用域既不显示也不可调用；原76项 input/output schema 保持一致 |
| 3 实例参数读取、写入与原子性 | Columns4→6，顶点24→36；另一实例和共享默认4不变。Offset ATTRIBUTE(position) 使实际宽高1→2；VALUE[0,0,1] 清除属性模式并使 Z=1。现场 RNA 菜单 A/B 映射真实整数，宽度1→3；Float/Bool/String/Color/Rotation/Object/Collection 引用读回；Material 空引用及错误引用类型验证；错误批次不留下部分写入 |
| 4 求值几何、图片、复合迭代 | 实际顶点/边/面、世界包围盒、完整 SHA256 顶点签名、临时 mesh 清理；正实例夹具报告1个实例、9个点及包围盒、无错误。视口和参数迭代返回原生 MCP ImageContent，收到的字节与 PNG/SHA256 完全一致；AI 已读取实际图像 |
| 5 精确 GN 编辑器截图与布局 | 2100×1193 原生窗口 framebuffer；ALL/SELECTED、指定 Frame、sidebar 开关；实际图像可读，显示 Group Input、两个 Grid、Frame、Reroute、Join、Group Output 与连接。结构证据包含父 Frame、位置、绘制后尺寸、socket 与多输入 links；捕获恢复区域类型、树/pin/path、选择和 sidebar |
| 6 原生 Reroute 与有界整理 | 同一夹具单 link、同源一对多、独立来源 multi-input 插入/移动/删除：counts、bounds、签名和端点/多输入顺序等价；真实截图显示插入点；静音/排序/回滚失败离线回归通过。仅移动明确测试节点，不自动整理原案例 |
| 7 审计、路径、保护与兼容 | 保留既有审计/超时/Project 路径限制；无可选注释插件依赖。原总工程 SHA256 与初始基线一致；本次重新启动后的原案例未载入。旧76项 schema 和旧61项兼容基线均通过；保护证据边界见下文 |
| 8 测试与交付 | 离线、正式协议、5.2.1 实机报告分开保存；README、用法/API、profile 示例、开发日志、验收脚本和本完成审计均已更新 |

证据在 `.runtime/algorithm_review_profiles/report.json`、`.runtime/algorithm_review_live.json`、`.runtime/algorithm_review_extended_live.json`。
两个实机报告当前 `ok=true`；主报告 checks/images 含多次续验历史累计，不当成独立测试数量。
最新检查：本次增强及相关 bootstrap/profile 离线组合 **243项通过**；旧单元、Frame、打开工程、Outliner、Scene 隔离 **293项按各自独立进程通过**。
将这些旧模块混在一个进程时，3个“服务端不导入bpy”的断言受其他模块的 fake bpy 污染失败；独立运行和真实 stdio 服务验证通过，没有通过放宽断言掩盖问题。部分旧测试退出时仍有 ResourceWarning，记录于 `.runtime/test_*_final.log`，未作无关重构。
正式 profile 与旧76项schema、旧Outliner61项schema兼容复查通过；`git diff --check` 通过，仅换行格式提示，未 commit/push。

本次改动范围：`blender/properties.py`、`geometry_nodes_edit.py`、`model_inspection.py`、`geometry_nodes_capture.py`、`review_capture_common.py`、`review_maintenance.py`；对应 `blender_mcp/tools` 独立模块、`domains.py`、`tool_profiles.py`、app/注册和 bootstrap 接线。现有 `tools/geometry_nodes.py` 仅修复省略默认值序列化。新增相关离线/正式stdio/实机测试、profile 示例；更新 README、用法、能力盘点、验收状态、开发日志及测试入口。工作树还含此前打开工程/Outliner等改动，保留而未作为本次新增内容。
真实 IDPropertyArray 与 list 的比较/快照复制、RNA instance iterator 条目生命周期均已修复并实机复测。
省略接口 default 不再注入 null，保留 Blender 原生菜单/数值默认；显式 null 数据块默认仍保留，旧工具 schema 不变。
截图默认优先最大的合适区域，初次绘制后取景，并等待平滑导航结束后的实际绘制；不修改用户导航偏好。

## 保护证据与已知限制

- 原文件 SHA256：`10add4ebaf3ea4b711a735620002547fd31c9e7847e679d7a9875057a3d2469d`。文件完整字节一致，持久案例内容没有被覆盖。
- 旧 PID127096 中曾只读核对36对象一致、30/31主 GN 完整一致；另一组4个横坐标差±1，其余计算图/参数/引用一致，原因未归因。原会话已关闭，不能声称最终重新核对了旧会话未保存内存状态，也没有擅自覆盖该布局差异。
- 内部 GeometrySet 全部曲线/点云/体积不能由 evaluated Object.data 完整暴露，不可读取组件明确 unknown。实例只统计依赖图暴露条目；空 mesh 不等于空模型。
- Material/Image 非空引用没有本次实机正向样本；Material 空引用/错误类型实测，引用范围与类型校验另有离线/API证据。非连续菜单10/42是离线验证，实机 A/B 为现场枚举0/1；没有将其混称。
- Node Editor 的 View2D 平移/缩放无法保证精确恢复，原有画布默认 KEEP；明确 fit 会改变画布视图。尺寸是 UI 缓存，不代表未绘制节点也可靠。
- 视口图片保留真实场景其他对象；测试网格重叠的深度冲突也如实显示。尺寸/拓扑结论来自求值数据，不只依据图片。
- Reroute 是曲线锚点，不承诺正交布线；复合迭代在参数成功后若截图失败，明确报告 parameters_committed，不伪报参数回滚。
- GUI 未绘制或捕获失败时不返回旧/空图；无头模式可读参数和几何，不能返回本工具要求的真实 UI 图片。

## 以下为历史过程记录（不代表当前等待状态）

日期：2026-10-05。历史 Blender PID127096，5.2.1 LTS，交互式 GUI。

## 已完成

- 现有能力盘点：原76个MCP工具/72个bridge命令，参数、Frame与渲染预览部分已有；严格实例输入、真实UI截图与Reroute整理增强见能力盘点。
- 13个新MCP工具及1个固定维护工具已接入原domain/profile机制。最新实际server.py/官方ClientSession验证：all90，algorithm_review26，core20，outliner35，GN42，algorithm_reproduction57；旧76个input/output schema不变。禁用域不显示且不能调用。
- 最新离线组合233项通过。历史验证从208项逐步补充RNA区域指针、原生Window/ImBuf释放、模式/静音/编辑器恢复及枚举数值映射检查；离线结果与实机分开记录。
- 当前Blender已刷新到新增bridge命令。专用夹具位于Project/MCP验收，未保存或覆盖案例总工程。
- 实机参数：整型Columns实例4→6，实际求值mesh顶点24→36；共享默认4、另一对象输入4不变；错误identifier批次、类型及越界输入均拒绝且无部分写入。
- 实机Reroute：单link、同源一对多分支、独立来源multi-input三种情况的插入/移动/删除均通过；求值counts/bounds/signature与端点/多输入顺序保持等价。

## 尚未完成

- 真实视口/节点编辑器截图最终MCP回传和AI视觉读取，复合参数迭代截图。
- 最终原始36对象、31主GN组与总工程SHA256保护检查。
- Field/枚举/引用等类型的实机覆盖（离线验证不可代替实机）。

截图实测先发现Area RNA wrapper的Python identity比较失效，已改为as_pointer比较；随后发现图像buffer释放调用误写为imbuf.free，已按官方API改为ImBuf.free并新增原生分支测试。等待加载该修正后续验同一夹具。没有把失败截图作为成功图像返回。

详证：`.runtime/algorithm_review_profiles/report.json`、`.runtime/algorithm_review_live.json`。报告仅部分通过，当前ok=false，不能宣称整体完成。

## 后续维护与保护核对

- 增加固定模块维护工具 blender_reload_review_bridge；正式协议验证 all90/core20/outliner35/GN42/algorithm57，algorithm_review仍26，不开放维护写操作。旧76项schema不变。
- 更新后的离线组合214项通过；旧Outliner profile正式协议验证也通过（旧61schema保持）。
- 当前进程尚需首次装入维护入口及最终ImBuf释放修正；后续可具名MCP刷新，不承诺已在当前运行时安装。
- 总工程SHA256及36个原始Object快照一致。31个主GN组中30个完整一致；另一个组有4个节点横坐标相对初始基线变动±1，其余类型、defaults、接口、links、modifier引用均一致。差异报告 .runtime/review_asset_differences.json，原因未归因，不自动覆盖当前布局，也不宣称完整节点快照完全一致。

## 2026-10-05 后续源代码安全审查

- 原生 health 再次确认同一 PID127096、5.2.1LTS、原工程路径和活动场景；`reload_review_commands` 仍缺失。首次维护入口加载问题尚未解决，不重复触发截图或新增夹具。
- 审查发现旧属性模式实现仅写属性名，遗漏独立 type 开关。现改为现场生成 RNA 枚举确认、显式写模式开关、读回模式及数值；补动态隐藏检查和 5.2 点号 RNA 驱动路径。该修正待加载后实测，旧 Field 报告不再作为通过证据。
- Reroute 现保留有效静音状态；排序重建在删除 RNA link 前缓存静音，回滚恢复标签/显示状态并核对拓扑及顺序。顺序恢复失败不再误报 rolled_back=true。
- 截图准备始终 pin 指定树，以 edit_tree 指针确认实际显示树；恢复原节点类型、嵌套路径及材质 Shader 树，使用目标 area context。必要结构化证据读取失败时不发布图片，并恢复 UI。
- 离线组合 230 项通过（含 16 项新增安全测试）。正式协议清单与旧 schema 未因这些修正变化；真实视口/编辑器图片和 Field 模式实机验收仍未完成。

## 当前逐项完成审计（未完成）

| 目标 | 当前证据 | 尚需证据 |
| --- | --- | --- |
| 1 调查现状、架构、SDK及5.2.1 API | 能力矩阵、初始76工具/72命令快照；health精确5.2.1LTS/PID127096；mcp1.30.0、pydantic2.13.5；固定v5.2.1官方源码 | 最终Field/图像UI行为仍待现场确认 |
| 2 独立域、命名、既有profile兼容和服务端过滤 | 独立工具/bridge模块；all90、review26及四个原profile正式stdio实测；旧76schema不变；禁用域直接调用被拒绝；示例配置与刷新方式文档 | 无新增协议缺口；服务运行时维护入口尚未装入 |
| 3 严格实例参数及原子写入 | 实机Columns4→6、24→36顶点、共享默认/另一实例4不变、错误批次不写；离线类型/引用/歧义/范围/驱动/隐藏/模式与读回回滚测试 | 新type开关、动态枚举、Field/引用等类型的5.2.1实机覆盖 |
| 4 求值模型与真实视口图像/复合迭代 | 原生模型求值数据已实测，临时mesh清理代码与测试；capture-time视图/着色及modifier状态元数据；具名截图流程 | 原生ImageContent成功返回及AI读取、参数变更前后图像、复合迭代真实结果 |
| 5 GN布局、绘制尺寸、精准树、子集/sidebar截图 | 布局读取/Frame/Reroute/links及尺寸可靠性标记；离线精准目标、嵌套路径/Shader恢复；续验脚本增加ALL、SELECTED/sidebar和PNG内容哈希核对 | 同一夹具真实编辑器图片回传与AI视觉确认，现场子集/sidebar和绘制尺寸 |
| 6 Reroute单link/分支/multi-input插入移动删除 | 三类实机counts/bounds/signature、端点/顺序等价通过；追加静音、回滚失败和标签/显示恢复离线验证 | 加载新静音/回滚修正后复测，并确认图片中的布线 |
| 7 审计、路径、隔离及旧行为兼容 | 既有审计和Project路径限制；独有未激活夹具；旧schema及受影响模块测试；未保存案例总工程；无可选注释插件依赖 | 最终完整资产保护结论；初始31主GN快照仍有1组4节点±1布局差异，已保留且不自动覆盖 |
| 8 测试、文档和完整交付 | README、能力盘点、API/用法/profile/已知限制、开发日志、233项离线及stdio报告；续验门禁已实际拒绝缺少维护命令的当前进程 | 全部实机验收及最终交付报告；当前不能标记目标完成 |

最新门禁 `.runtime/algorithm_review_live.json`：缺少 `reload_review_commands`，`test_mutations_performed=false`、`saved_blend=false`。门禁在任何参数或拓扑写入之前结束，未创建新夹具。旧进程仍保留 imbuf.free 错误实现，不能通过再次调用截图验证磁盘上的修正。现无正在等待的测试进程；等待的是首次安装维护入口。
