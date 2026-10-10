# 项目开发日志

## 2026-10-05：按用户澄清改为目录浏览，全部工具常驻

- 用户明确域控制用于查看某个界面的工具列表，不是启用权限。此前过滤list/call的实现偏离需求，本次移除域可用性过滤与调用拦截。
- 所有profile及运行期阅读状态下tools/list固定93项；tools/call直接进入原业务与审计。目录锁只保护阅读状态。tools.listChanged=false，不再发送列表变化通知。
- blender_tool_domains新增domains/include_parameters查询，按实际注册定义返回说明和schemas；state与set仅管理文档阅读目录。replace/add/remove为正式阅读操作，enable/disable兼容add/remove。
- BLENDER_MCP_VIEW_DOMAINS用于初始阅读目录，旧LOCAL_DOMAINS与profile兼容；global-only/core只表示默认通用文档视图，仍开放全部93项。
- 原90项完整定义快照一致；正式目录单元/协议23项通过，审计参数88项、相关239项通过（包含16项重复目录单元）。两种profile协议脚本均全部93项，旧76/61schema一致。
- 首次混合测试311项中3个no-bpy隔离断言被既有fake bpy污染；日志保留，未改断言，独立进程通过。既有审计文件ResourceWarning保留。
- 独立官方Codex app-server读取已安装同一插件返回93项及listChanged=false，证据codex_tool_directory_status.json。当前聊天仍旧25项，不能把独立连接成功当成当前聊天已更新；已有旧MCP进程需一次重连装入新程序，此后浏览无需重连。
- 更新README、工具目录、架构、插件接入、检查文档和示例；旧动态域验收标历史，新增TOOL_DIRECTORY_ACCEPTANCE.md与经验文档“工具目录与可用性分离经验”并更新索引。
- 未启动/操作Blender，未保存或替换工程；未commit/push，修改保留在MCP Git工作区。git diff --check通过，仅换行提示。

## 2026-10-05：完成要求核查与交付

- 对照原附件逐项检查，完成SDK公开API、全局/局部完整分类、旧90定义、启动迁移、原生域控制、正式协议及回归证据核查；93行文档分类与源映射完全一致，两个新增启动示例可解析，插件源与缓存一致。
- 单元23项、正式动态协议7项及既有130/246/197项回归通过；正式SDK收到5次通知并重列更新。真实Codex未将新增工具应用至模型目录的限制已实测并如实交付，使用用户验收第6项允许的精确初始域/同步安装/重连后备分支。
- 原生选择恢复局部为空、服务端及模型目录25项一致；完成git差异和任务Markdown链接检查。完整核查记录 `.runtime/dynamic_domains/final_completion_audit.json`，说明行为证据和客户端限制，不以核查脚本代替行为测试。没有剩余必需实现/文档工作；无工程操作、子代理或commit/push。

## 2026-10-05：最终原生客户端限制与状态恢复

- 下一目标回合模型目录仍25项，原生状态仍Properties30/revision1，确认域状态跨回合保留但本机当前Codex未将新增工具应用到模型目录。未把发送通知或SDK重列成功冒充Codex即时刷新。
- 原生replace GN+Properties+model_inspection=51、enable node_frames=58、disable=51、幂等revision不变和global选择结构化拒绝均验收；clear后再次原生查询局部为空、清单25，模型目录也25一致。无Blender业务调用及工程操作。
- 将本机客户端限制、适用范围及唯一精确启动域/同步安装/重连后备写入工具域、接入、验收和经验文档；保留早期26项与active-writer历史证据。完整原生报告为 `.runtime/dynamic_domains/native_codex_refresh/report.json`。按用户验收允许的客户端限制分支交付，不宣称全部Codex版本不支持，也不缩减服务端动态功能。

## 2026-10-05：重启后真实聊天域控制验收

- 用户再次重启后，本聊天原生清单25项global-only，三个域控制工具已出现；原生域状态revision0、局部为空，域目录含全部10个局部域。此前旧26项门禁已消除。
- 本聊天原生replace Properties返回精确30项、revision1、notification_sent=true，无Blender业务操作。同回合两个调用边界的模型目录仍25，尚不能宣称即时刷新成功；保留选择到下一回合检查，再恢复局部为空。证据 `.runtime/dynamic_domains/native_codex_refresh/report.json`。

## 2026-10-05：继续验收——真实注册表启动检查与旧进程证据

- 只读进程检查显示正式server.py实例均20:24创建，早于20:50动态改造。旧26项清单与尚未重连的运行实例一致；不关闭进程、不绕过当前聊天写入锁，未将新连接结果当作当前聊天完成刷新。
- 在SDK协议初始化前读取公开实际注册表并核对完整分类，补builder未分类/缺失与绕过名称记录的注册测试；更新审计类注释，移除旧私有handler描述。原90项定义仍按改造前快照核验。
- 项目虚拟环境：动态单元23+正式stdio协议7=30项通过（144.913秒）；Outliner/文件打开/兼容回归246项通过（8.241秒）。两个旧profile正式协议脚本再次通过，76/61项旧schema一致；独立Codex验证安装插件25项及listChanged=true。
- 系统Python首次收集失败（缺少mcp），已改用项目锁定SDK的虚拟环境执行并通过；不将收集错误当作业务回归。补经验、接入和验收文档及进程证据。目标聊天需装入改造后实例并独立验证通知刷新；未commit/push、创建subagent或操作用户工程。

## 2026-10-05：动态工具域实现、验证与经验归档

- 用完整业务注册表及SDK正式handler替代启动时物理删除；global25常驻，10个local域68项，完整目录93。新增域目录、状态及replace/enable/disable控制工具；未分类注册失败关闭，清单与实际调用同步授权，失败/取消回滚，stdio单客户端进程隔离。
- 默认和插件源为global-only，旧命名profile只定义初始域；补精确启动JSON数组后备。原90项工具名称、说明及input/output schema与改造前快照一致，未重写Blender业务，不新增关闭/重启或任意脚本接口。
- 最终动态单元21项、生产stdio协议7项共28项通过（144.348秒）；正式SDK客户端收到5次通知并重列缓存。既有保护回归分别130、242、197项通过（套件重叠）；旧profile协议脚本验证原76/61项schema兼容。完整证据及命令见DYNAMIC_DOMAINS_ACCEPTANCE.md。
- 已同步官方插件安装；源与安装缓存global-only配置SHA256一致。独立Codex验证已安装插件25项、三个控制工具及listChanged=true，不等同于目标聊天即时刷新。
- 持续连接客户端探针遇到已有聊天active writer，未绕过锁、创建聊天或启动模型回合；保留失败报告。经验整理时本聊天仍是旧algorithm_review26项，同次原生server_status为bridge_unavailable/Windows10061；目标聊天刷新与桥接监听分别待核验，旧清单根因未定位。
- 按用户要求在经验文档新增“BlenderMCP动态工具域与客户端刷新经验.md”，补索引及历史文档版本提示，防止core20/all90旧说明被当成当前规则。文档整理仅核对证据、链接及差异；未启动Blender、打开/保存工程，未commit/push或创建subagent。

## 2026-10-05：重启后验收与经验文档

- 用户再次重启后，本聊天原生Blender工具清单实际为20项core，插件配置更新验收完成。
- 同次原生health返回bridge_unavailable/Windows10061，9876桥接未监听；明确记录插件配置
  生效与Blender运行连接为独立状态。本轮仅文档工作，未启动Blender、打开或保存工程。
- 按用户指定目录新增“经验文档/BlenderMCP插件配置缓存与重启排障经验.md”，补充经验索引，
  并为旧启动排障文档添加后续结果，保留历史原始记录。同步更新本仓库接入文档与开发日志。
- 经验包含根因、备份与官方CLI更新步骤、目标聊天验证、三种刷新操作区别及本次profile范围。
  文本变更只做内容、链接及git diff检查，不重跑实现测试；未commit/push。

## 2026-10-05：修复重启后插件仍加载 all 的安装配置

- 用户反馈重启后仍为90工具。核对发现插件源 `.mcp.json` 已设core，而已安装的
  Codex缓存0.1.0缺少 `BLENDER_MCP_PROFILE`，因此服务按兼容默认all启动。
  本聊天原生health成功，实际5.2.1LTS/PID154996，新Properties/Reroute/截图命令均已装入。
- 先在忽略的 `.runtime/plugin_profile_repair/` 备份原安装配置和用户配置，
  再通过官方 `codex plugin add blender-mcp@blender-local --json` 刷新同一插件。
  未卸载插件、删除缓存、修改Blender工程或重启Blender。
- 核验缓存及 `codex mcp get blender --json` 均为core；临时独立Codex app-server
  实际初始化该插件并返回20项工具，验证退出0。证据为
  `.runtime/codex_plugin_core_installed.json`、`.runtime/codex_plugin_core_status.json`。
- 当前聊天仍持有更新前90项清单；待重新连接/完整退出重开Codex后在原生聊天核验20项及health，
  不把独立进程结果冒充当前聊天已切换。无需手动bootstrap。core不包含建模能力，使用方法明确其范围。
- 更新插件接入文档及core示例数量20；未commit/push。

时间按 Asia/Shanghai 记录。每次实质变更应记录目标、变更、验证和遗留事项；测试未完成时不得写为已通过。

## 2026-10-03：第一版开发

### 需求与决定

- 优先接入本地 Codex，以几何节点进行可编辑的参数化建模。
- 支持原始网格上的形态键，以及对象、形态键和节点组相关驱动器。
- 使用外部 Python MCP 服务与 Blender 内部桥接组件分离的结构；不需要修改 Blender 源码。
- Blender Python 操作在主线程执行，本地桥接只监听回环地址。
- 提供明确的节点、插槽、参数和驱动器工具，避免把任意 Python 执行作为主要接口。
- 用户已将程序目录修正为 `Version/5_2_1_LTS`。
- MCP 纳入 Git 管理，并记录安装、使用、架构和验证证据。
- 用户明确目录职责：`Version/` 为 Blender 分发程序，`Project/` 为 Blender 工程与输出，`MCP/` 为 MCP 源码、Git、文档、日志、测试和运行环境。Git 仓库已迁至 `MCP/.git`，主分支为 `main`。

### 环境检查

- Blender 可执行程序实际报告 `5.2.1 LTS`。
- 开发环境 Python 为 `3.12.7`，MCP 依赖使用项目内独立虚拟环境。
- 本机已安装 Codex 命令行和 Git。

### 首轮实现时的阶段状态（历史记录）

- 主体实现与自动化验证进行中。已完成首轮 54 项服务端单元检查；真实 Blender 验证仍在修复与重跑，尚不能视作最终验收。
- 尚未记录功能验收通过，尚未注册新的 Codex MCP 服务。
- 功能验收后将追加实际命令、结果及限制。

### 审查重点

- 驱动器更新失败时是否保持原有表达式和变量。
- 形态键首个变形键是否同时建立 Basis，坐标格式错误时是否避免部分写入。
- 连接超时是否释放连接、清空残留数据，并保持变更请求不自动重放。
- Blender 5.2 修改器输入的实际存储与求值是否正确。
- 几何节点组复用、对象引用等功能是否有可用的结构化引用方式。

### 2026-10-03 追加：实现完成与功能验收

目标：交付第一版可用的 Blender MCP（几何节点参数化建模优先，形态键与驱动器齐备），并在本机完成真实验证。

变更（新增，未改动 `Version/`、MSI、用户目录、`.codex/`、`AGENTS.md`）：

- `MCP\server.py`、`MCP\blender_mcp\`（config/bridge/models/tools：meta、scene、geometry_nodes、shape_keys、drivers、launcher），FastMCP stdio，共 **38 个工具**。
- `MCP\blender\blender_mcp_addon.py`（单文件，仅 `bpy`+标准库）：非阻塞回环 socket、按行 JSON、令牌鉴权、
  主线程 `bpy.app.timers` 轮询、面板与操作符、全部命令实现。
- `MCP\blender\bootstrap.py`（GUI/`--python`/无头 `--mcp-pump`/控制台四种启动路径，复用健康桥）。
- `MCP\launch_blender.ps1`（默认 GUI、`-Background` 无头、复用既有桥、从不杀进程、`-IsolateConfig`）。
- `MCP\tests\`（单元 56 项、真实 Blender 集成 48 项、MCP stdio 端到端 78 项、`run_all_tests.ps1`、`probes\`、`reports\`）。
- `MCP\README.md`（中文）、根 `README.md`、根 `.gitignore`、`MCP\.gitignore`。
- 产出：`Project\demo\parametric_demo.blend`（可编辑节点组 + 形态键 + 4 处驱动器；重开后仍可调参）。

验证（实际命令与结果见 `tests\reports\TEST_REPORT.md`）：

- `MCP\.venv\Scripts\python.exe tests\test_unit_validation.py` → `Ran 56 tests ... OK`。
- `Version\5_2_1_LTS\blender.exe --background --factory-startup --python-exit-code 1 --python tests\blender_integration.py`
  → 48/48 通过、exit 0（连续两次运行均通过，产出 .blend 689569 字节）。
- `MCP\.venv\Scripts\python.exe tests\test_mcp_stdio.py` → 78/78 通过、exit 0，含 22 项负例、错误令牌、死端口与进程收尾。
- `MCP\launch_blender.ps1 -Background -FactoryStartup -IsolateConfig` → 桥就绪；再次运行 → 复用既有桥且不启动新进程。

审查重点答复：

- **驱动器更新失败**：`update_driver` 先静态校验（`ast.parse` + 白名单）与变量解析，再快照原表达式/变量；
  若 Blender 仍判无效则**恢复原表达式与变量**（实测 `phase * 2` 恢复成功、`is_valid=True`、求值不变）并报 `invalid_expression`。
  `add_driver` 新建后若无效则**删除本次新建**并报错（原子）。
- **形态键**：首个变形键会**先显式建立 `Basis`** 再建目标键；`Basis` 默认写保护；
  坐标载荷**先整体校验再写入**（实测中间项非法时前后采样完全一致，无部分写入）。
- **连接超时**：超时/协议错误时**关闭连接并清空读缓冲**，变更请求**不自动重放**（单元测试断言 socket 置空、
  缓冲为空、服务端仅收到 1 次请求，且下一次调用重连后正常）。
- **Blender 5.2 修改器输入**：实测为 `modifier.properties.inputs[identifier]["value"]`（IDProperty），
  写后必须 `object.update_tag()` + 依赖图更新才会重算，已验证 `Density/Size/Height` 改动会改变求值顶点数与包围盒。
- **结构化引用**：对象按名称（错误信息附带可用名称列表）、节点按名称或索引、插槽按 **identifier** → 名称 → 索引；
  节点组可复用（`attach_geometry_nodes_modifier(reuse_existing)`、`create_node_group(if_exists='reuse'|'replace')`）。

遗留事项：

- `batch_graph_edit` 无事务/回滚（按设计，响应显式声明 `rollback: false`）。
- 驱动器变量仅 `SINGLE_PROP` / `TRANSFORMS`；独立生成的几何可能忽略原始网格形态键，处理原始网格的节点流可保留变形。
- 未做：GUI 交互测试（按要求未启动 GUI）、Codex 会话内真实调用（仅验证到 MCP 协议层）、非 Windows 平台。
- 本机受限环境的两处适配记录在 `TEST_REPORT.md` 第 6 节（虚拟环境 `sitecustomize.py` 临时目录权限 shim、
  stdio 客户端传输改造）；正常机器上可移除/切回 `BLENDER_MCP_FORCE_SDK_STDIO=1`。
- `.runtime\tmp\` 下遗留若干由早期 pip/ensurepip 失败产生的空目录（0o700，受限环境拒绝删除），
  位于已忽略的运行目录内，可手动清理。

### 2026-10-03 追加：审查修复、最终复核与 Codex 注册

- 加入明确的数据块引用格式，支持嵌套几何节点组、Object Info 及组接口/修改器指针参数。
  更换节点组导致 Blender 重建插槽和丢失链接时返回真实链接数与提示；详见 `GN_REFERENCES.md`。
- 形态键坐标拒绝非有限数值；驱动变量拒绝重名或非法名称，失败的驱动器替换/更新恢复旧状态。
- Blender 主线程轮询限制客户端数、读取字节和请求数，并在后续轮询继续处理已缓冲的请求。
- 桥接仅接受 IPv4 回环地址；工程目录覆盖仅可选择同级 `Project/` 内子目录。
- 修复发送故障的错误转换、慢速回复的总期限、无头运行时长解析和测试进程归属检查。
- 依赖固定为 `mcp==1.30.0`。临时安装用 `sitecustomize.py` 已保留为停用备份，正常运行不加载。
- 正常用户账户下重新运行：56 项单元、7 项连接与配置回归、48 项 Blender 集成、41 项引用等回归、
  78 项官方 SDK stdio 联调，**230 项全部通过**。完整证据见 `tests/reports/TEST_REPORT.md`。
- 已用 `codex mcp add blender` 注册本地服务，并核对服务启用、Python/入口路径和 Blender 版本路径。
  注册配置模板为 `MCP/codex-config.toml`，本机应用登记由 Codex 自身保存。
- Git 仓库位于 `MCP/.git`，分支 `main`。没有执行提交、推送或发布。
- 根目录 README/忽略规则已在 `MCP/docs/history/` 完整保存并校验哈希。
  自动审批拒绝移除根目录两个副本，已向用户请求明确授权；在收到答复前保留原文件。

## 2026-10-03（第二版）：审计日志 / 实时看板 / 事务批量编辑 / 截图预览

### 需求（用户原文要点）

1. 为**每一次** AI MCP 操作保留持久日志：参数、结果、错误、耗时；批量编辑的每一步与回滚。
2. 提供一个**实时显示日志的窗口**。
3. 批量事务回滚要真正有用。
4. 能通过 MCP 截图模型预览，并把**图片本身**返回给 AI。

### 根目录清理授权（已接受）

- 用户明确授权 Codex 删除工作区根目录（`D:\AI\MCP\Blender\`）下的 `README.md` 与 `.gitignore`，
  由 Codex 负责架构与集成；本版**未触碰**工作区父目录及根目录任何文件。
- 这两个副本的完整内容此前已保存于 `MCP/docs/history/`（见上一节记录）。

### 变更

- `MCP\blender_mcp\audit.py`（新增）：JSONL 审计日志。每进程一个文件
  `audit-<source>-<pid>-<session>.jsonl`；UTC 毫秒时间戳 + session/request id + 递增 seq；
  脱敏（token/secret/password/… 键 → `***`，真实 token 文本替换，base64/bytes → 长度 + sha256）；
  限长（字符串 320、数组 20 项 + sha256、深度 5、键 64）；按大小轮转并保留归档；写失败只打一条限频 stderr 告警。
- `MCP\blender_mcp\audit.py` + `tools\__init__.py`：`AuditedFastMCP` 覆写 SDK 的 `call_tool`，
  在**进入工具之前**记录 `tool_start` 并在结束时记录结果/错误/耗时——包括 SDK 参数校验失败
  （`blender_object_info` 缺参这类）也在审计里留痕。桥调用被透明包装：参数里携带保留字段 `_mcp_audit`
  以实现两侧请求号关联，且仅在运行中的插件声明 `audit_protocol>=1` 时才注入。
- `MCP\blender_mcp\log_viewer.py`（新增）+ `MCP\launch_log_viewer.ps1`（新增）：
  标准库 HTTP 只读看板（仅回环，仅 GET，无 CDN，中文界面），支持状态/来源/关键词过滤、自动滚动、
  逐行展开参数与结果、可选 SSE；`--port 0` 由系统分配端口并原子写入 `viewer-<pid>.json`，并发启动不冲突。
- `MCP\blender_mcp\tools\operation_log.py`（新增）：`blender_operation_log`（读取审计事件，可过滤）
  与 `blender_log_dashboard`（启动/查询/停止看板并返回 URL，默认不开浏览器）。
- `MCP\blender_mcp\tools\preview.py`（新增）+ 插件 `capture_preview` 命令：`blender_capture_preview`
  返回 **MCP image 内容**（PNG）+ 元数据；`auto/viewport/render` 三种模式、5 种预设视角、按求值后包围盒取景、
  2048 px / 3 MiB 上限、只写 `Project\previews\*.png`、失败也恢复渲染设置与临时数据块、绝不自动保存 .blend。
- `MCP\blender_mcp\tools\geometry_nodes.py` + 插件：`blender_batch_graph_edit` 默认
  `transactional=true`。实现为「隔离草稿 → 全成功才替换数据块身份 → 重定向修改器 / 嵌套 Group 节点 /
  驱动器目标 → 校验 `users==0` 才删除旧数据块」；失败删除草稿、原节点组零改动；
  `continue_on_error` 只允许与 `transactional=false` 同时使用；链接库节点组直接拒绝。
- 插件 `dispatch` 记录每个桥命令的 `command_start`/`command_end`（含参数校验失败标记），
  `health` 增加 `audit_protocol` 与审计状态；`MCP\blender_mcp\tools\__init__.py` 的指令文本同步更新。
- 新增测试：`tests\test_audit_features.py`（25）、`tests\blender_feature_integration.py`（54）、
  `tests\test_mcp_stdio_features.py`（37）；更新 `test_unit_validation.py`（工具/命令清单、schema、
  事务默认值，56 → 58 项）、`test_mcp_stdio.py`（必需工具、事务断言、回滚负例，78 → 85 项）、
  `blender_integration.py`（事务提交断言，48 → 49 项）、`tests\run_all_tests.ps1`（8 个套件 + 容错汇总）。

### 实测（本机 5.2.1 LTS，详见 `tests\reports\TEST_REPORT.md`）

- `test_audit_features.py` → 25/25 通过。
- `blender_feature_integration.py` → 54/54 通过（含回滚后原节点组逐字段一致、提交后修改器参数/驱动器/嵌套引用/
  节点组自身动画保留、真实 PNG 尺寸与非空像素、场景状态完全还原、审计轮转）。
- `test_mcp_stdio_features.py` → 37/37 通过（官方 MCP stdio 客户端、事务回滚为错误、看板 URL 可访问）。
- 完整运行 `tests\run_all_tests.ps1`：记录在案 350 项检查，新增 116 项全部通过；基线部分 **6 项**因
  「禁止写 `MCP` 之外路径 / 禁止 `chmod`」失败（3 项 `tempfile` 清理、3 项写入同级 `Project`），
  与本次改动无关，正常账户环境不受影响。

### 遗留事项

- GUI 视口截图（`mode=viewport`）只能在有窗口的 Blender 中人工确认；本次仅覆盖了无头下的可执行报错路径。
- 事务成功会替换节点组数据块身份（名称/接口 identifier 不变），bpy 之外缓存旧数据块的代码不会被改写；
  这不是 Blend 文件级撤销。
- 首次渲染需要初始化 GPU 管线（本机实测首帧约 20 秒，之后同进程 <1 秒），因此默认超时 45 秒。

## 2026-10-03（第二版 · 验收修复增量）

基线提交：**`18c300e`**（Codex 建立）。工作区根目录 `README.md` / `.gitignore` 的删除已获用户明确授权，
历史副本保存在 `MCP/docs/history/`；本增量**未触碰**工作区根目录与父目录任何文件。
日志/测试报告写在 `MCP` 内，模型输出写在 `Project`，`.runtime/host_*` 与 `Project/previews` 中的
Codex 样本文件均未改动。

### 修复项（均针对 Codex 的集成评审）

1. **看板计数与 UI 状态**
   - 新增 `audit.annotate_operations()`：把 start 与 end 按「来源 + 会话 + 请求号 + 命令名」配对，
     只有**仍在活动会话中**且没有对应 end 的调用才算 `running`；已结束会话里未闭合的 start 归为 `info`。
     看板读入窗口 1500 条事件用于配对，展示切片仍受 `limit` 限制。
   - 四色计数明确是**操作**数（一次 MCP 调用与其桥命令各算一项），并显示"读入/返回/匹配/info/已结束会话"。
   - 页面改为**按行 key 复用 DOM**：未变化的行不重写、展开状态保留、`follow` 未勾选时不再强制滚动。
2. **关联能力恢复**：桥包装器在首次探测失败后会重新武装——传输失败（`bridge_unavailable/timeout/protocol_error`）
   或 `health` 报告 `audit_protocol` 时恢复；新增审计字段与事件 `bridge_audit_unavailable`（`correlation="unavailable"`）
   与 `bridge_audit_regained`（`correlation="restored"`），`bridge_ok` 带 `correlation=linked|unavailable`。不重放任何请求。
3. **预览取景与场景一致性**
   - 相机改为**投影拟合**：把包围盒 8 个角变换到相机空间，`sensor_fit='HORIZONTAL'` 下
     `ortho_scale = max(投影宽, 投影高 × 宽/高) × 1.12`，并用投影深度设置裁剪面。相机矩阵由
     `location + rotation_euler` 直接构造（临时场景里的相机 `matrix_world` 不会被当前视图层刷新，
     之前用它会得到错误的拟合与裁剪面，导致模型被裁掉/画面空白）。
   - 临时渲染场景复制**源场景的帧/子帧/fps/fps_base/起止帧与场景自定义属性**，因此 GN/驱动器/动画
     在渲染时与元数据一致（实测 frame22 的 22 单位立方体渲染尺寸随帧变化）。
   - 默认预览只扫描**当前视图层**的可见网格，不再把其它场景的对象拉进来。
   - `view="current"` 不再被改写成 isometric：沿用它复用场景相机变换的既有分支。
   - 视口截图的状态快照/恢复补齐**选中集、活动对象、子帧、fps/fps_base**（成功与失败都恢复）。
   - 纯实例几何（GN 只输出实例）改为从 `depsgraph.object_instances` 取包围盒（有上限与截断标记）；
     若仍无法测量，则报带 `code_hint="realize_instances"` 的可执行提示。
4. **预览发布原子化**：去掉"先删除旧文件再 `os.replace`"的做法，直接原子替换；失败时旧图仍在，
   错误信息写明"previous file, if any, is untouched"。
5. **事务提交失败恢复**：改用 Blender 原生 `ID.user_remap()`（可覆盖手工枚举不到的引用）并保留手工枚举作为回退；
   提交过程中的任何失败（交换失败、残留引用、**修改器输入无法还原**、旧数据块无法删除）都会整体撤销：
   反向 remap、恢复 `use_fake_user`、还原名称、删除草稿，并**报错而非报告 committed**；
   成功时回报 `remap_mode`、`fake_user_preserved` 与重定向统计。

### 实测（本次增量，只跑受影响套件）

- `tests\test_audit_features.py` → **30/30 通过**（新增 5 项：待处理计数、看板操作计数、看板 UI 状态、
  初始探测失败后的关联恢复、legacy 部分批次 `ok=false`）。
- `tests\blender_feature_integration.py` → **64/64 通过**（新增 8 项：源帧渲染、22 单位立方体完整入镜与四边留白、
  竖幅取景、current 视图语义、当前场景限定、视口状态恢复、实例几何或 realize 提示、提交故障注入保留
  fake_user/修改器参数/无草稿泄漏，并验证故障后仍可正常提交）。
- `tests\test_mcp_stdio_features.py` → 见 `tests\reports\TEST_REPORT.md`（本轮改动触及 MCP 桥包装器，故重跑）。
- 未重跑：既有的受限沙箱基线套件（写 `MCP` 之外 / `tempfile` 清理）——按要求不再追这类环境错误。

## 2026-10-03（Codex 最终集成与验收）

- 按用户授权删除工作区根目录的 MCP `README.md` 与 `.gitignore`；完整历史副本位于 `MCP/docs/history/`。Git、代码、文档和操作日志均保留在 `MCP`，模型与预览在 `Project`，程序在 `Version`。
- 建立本地 Git 基线 `18c300e`；本轮审计日志、实时看板、默认节点组事务和截图预览作为后续本地功能提交，无推送。
- Codex 集成评审后补强事务失败恢复：在原生 `ID.user_remap` 前标记重定向模式，以便部分失败时反向恢复；反向重定向后再次还原修改器输入，并报告未恢复项。
- 本机正常账户完整运行 8 套测试，**371/371 全部通过**，使用官方 MCP SDK stdio 传输，真实 Blender **5.2.1 LTS**，运行器退出码 0。早期受限构建环境错误作为历史保留，已完成实际环境验证。
- 在独立 GUI Blender 中通过真实 MCP 调用验证 960×720 视口截图与相机渲染，均返回原生 `ImageContent`；截图前后选择状态、活动对象、视角、帧、渲染设置、相机与数据块数量全部一致。样本保存在 `Project/previews/`，证据在 `MCP/.runtime/host_*`，均不入库。
- 只读实时看板通过 API 确认运行，读取 `MCP/.runtime/logs`；工具累计 **41 个**。模型截图不自动保存 .blend。
- 用户明确要求**不要 computer-use**。后续 Blender 操作、检查和模型预览只通过 MCP / Blender API 完成，该偏好已写入 README 与架构文档。

## 2026-10-03（Codex 注册复核与椅子任务检查点）

- 用户要求先将 MCP 接入 Codex，随后自行重启。通过 `codex mcp add blender` 注册并核验 `enabled=true`；解释器与 server.py 均位于 `MCP`，Blender 路径为 `Version/5_2_1_LTS/blender.exe`，桥端口保持 9876。
- 官方 SDK 实际初始化服务并列出 **42 个工具**，包含批量编辑、预览、日志看板与新增 `blender_present_object`。
- 新增显示对象工具通过 Blender API 保留选择与取景，可调用 Windows API 请求激活当前 Blender 进程的自身窗口；不使用 computer-use 或模拟输入。该功能不修改几何，GUI 成功路径待实际验收。
- 原单元套件 58 项通过；新增 `tests/test_presentation.py` 验证无效视角、非法标志、无视口时不会激活窗口的三条保护。
- 参数化木椅任务保留：尚未开始模型写入。已委派生成仅通过具名 MCP 工具调用的分阶段配方，范围为 `examples/parametric_chair.py` 与 `docs/PARAMETRIC_CHAIR.md`；配方未运行、未验收，重启后先评审再执行。
- 本次尝试的 9877 GUI 进程在桥启动后即退出（退出码 0），未创建模型。没有将 Codex 指向这个临时端口，重启后需要启动并确认实际 GUI Blender 桥，再建模、验收前台显示和保存。

## 2026-10-03（参数化椅子实际完成）

- 重启后使用独立配置目录、`--factory-startup` 和 bootstrap 脚本启动实际 GUI Blender 5.2.1，桥连接保持 9876；官方 SDK 读取 health 确认 `background=false` 与当前进程 PID。
- 评审并实际执行委派的 MCP 配方：inspect → seat → legs → back → finish → verify → save。所有模型写入均为具名 MCP 调用，4 个图编辑事务批次（21、64、100、33 步）全部提交成功；多输入 Join 链接由 1→5→10，未丢失部件。
- 完成单对象几何节点椅子：65 节点、144 链接，座面、4 椅腿、2 根后倾靠背柱、3 根横档；8 个尺寸/角度参数可编辑，未应用修改器。默认尺寸与算式见 `docs/PARAMETRIC_CHAIR.md`。
- 9 项真实几何验证全部通过：座宽由 .48 改为 .60，宽度增加 .12；座高由 .45 改为 .52，模型顶部增加 .07；参数均已复位，包围盒与预测吻合、椅脚最低 Z 为 0。
- MCP 返回 960×720 原生 PNG 模型预览；已检查图像，椅子完整入镜。保存 `Project/chairs/parametric_chair.blend`，794,766 字节、首次保存不覆盖；预览在 `Project/previews/parametric_chair.png`。
- 实际 GUI `blender_present_object` 成功选择、隔离、框选椅子并保留修改器面板。最初依用户要求请求置前，Windows 拒绝 Blender 自身请求后，经限定本进程窗口的宿主窗口 API 激活成功；没有模拟键鼠或使用 computer-use。
- 用户随后说明正在打游戏、不要置前。立即停止前台激活；后续显示调用均 `foreground=false`，保持后台建模和保存。README 已记录当前偏好。
- 配方与说明由 Codex 集成并纳入本地 Git；DSH 完成会话已归入 MCP 工作区。审计与阶段输出保存在 `MCP/.runtime`，不入库。

## 2026-10-03（花瓣灯创意模型与数值插槽修复）

- 用户已结束游戏并明确请求将 Blender 置前。通过限定当前 Blender 进程的 Windows API 请求激活窗口；建模全程通过官方 MCP SDK 具名工具，不使用 computer-use。
- 自主设计花瓣灯：扁椭球底座、弯曲 Bézier 灯杆、径向椭球花瓣和中心球形灯泡；六个几何节点参数为 Height、Stem Bend、Petal Count、Petal Length、Opening、Base Radius。原椅子保留。
- 委派仅写 `examples/petal_lamp.py` 与 `docs/PETAL_LAMP.md` 的客户端配方。Codex 评审并实际执行，补上平滑着色、节点依赖布局与展开角的直观映射（X 倾角 = pi/2 - Opening）。
- 花瓣初次事务因 INT→VALUE 被桥拒绝而整体回滚。实际 Blender 浮点插槽类型是 VALUE，既有数值集合遗漏此类型；补入 VALUE，并新增 INT→VALUE、VALUE→VECTOR 及非数值拒绝断言。58 项单元测试全部通过。
- 通过 MCP 保存完整工作检查点 `Project/models/petal_lamp_working.blend`，仅重启本任务启动的 Blender 实例，加载修正后的桥并恢复工程。后续花瓣编辑成功，Join 多输入最终 4 条，没有丢失底座、杆或椅子。
- 真实模型验证六项全部通过：7→9 花瓣使顶点数 4542→5506；Height 1.10→1.35 使最高 Z 增加 .25 m；Opening .65→.95 改变求值指纹；三项均复位成功。
- MCP 返回 960×960 原生图片预览，已检查完整造型。最终工程保存至 `Project/models/petal_lamp.blend`，预览在 `Project/previews/petal_lamp.png`；检查点保留便于恢复。模型未烘焙或应用几何节点修改器。
- 模型当前是造型验证，预览使用 Workbench，未新增发光材质或真实照明。阶段日志 `MCP/.runtime/lamp_*.log` 与操作审计不入库，代码/说明纳入本地 Git。

## 2026-10-03（补做 Codex 本地插件实际安装）

- 用户指出 Blender MCP 未实际出现在 Codex。纠正前次验收口径：独立 MCP 配置登记和 Python SDK 连通，不能证明桌面插件安装或当前聊天原生工具可用。
- 在 `MCP/codex-marketplace/` 增加本地插件目录、清单和 MCP 启动配置，用官方 `codex plugin marketplace add` 与 `codex plugin add` 安装 `blender-mcp@blender-local`，显示名称 Blender MCP。
- 安装缓存由 Codex 管理；源文件、Git、文档、运行证据均在 MCP，未在父目录创建文件。备份配置后移除旧同名独立登记，避免覆盖插件提供的服务。
- Codex app-server `plugin/installed` 确认 installed/enabled 为 true；`mcpServerStatus/list` 确认 pluginId 匹配、初始化成功、toolsError=null、42 个工具。真实响应留在 `.runtime/codex_plugin_*.json`，不入库。
- 当前已打开聊天尚未验证原生 Blender 工具，桌面刷新/重启后仍需直接调用 health 验收。不再将独立 SDK 成功表述为当前 Codex 会话已接入。未改 Blender 模型或桥行为，因此不重跑建模测试。

## 2026-10-04（连接诊断、GUI 启动缺陷与独立案例工程）

- 首次当前会话原生 MCP probe/health 证明注册与 server 正常，9876 连接拒绝，进程/监听检查无 Blender。具名 launch 启动 120 秒后台诊断，health/ping/list_commands 成功，实例自行退出。
- 实际 GUI 验证进一步定位 bootstrap 的 SystemExit(0) 在 timer 注册后退出 Blender；成功入口改为正常返回，后台启动显式 --python-exit-code 1。启动日志保留修补前 READY/退出 0 与修补后持续在线证据。
- probe 保留阶段、错误码和恢复提示；启动前查进程/监听，拒绝鉴权、协议、超时故障时重复启动，启动失败为 isError，带 PID/日志。Windows 端口检查复用 5 秒连接预算，避免 0.5 秒误判空闲端口。
- Windows helper 使用同一 AuditedFastMCP 具名、类型化入口独立启动；避免临时官方 SDK stdio 客户端 Job Object 结束时回收其 Blender 子进程。不更改 SDK，不禁用鉴权，不调整端口/token/Blender 配置。
- 委派 dsh 仅实现场景工具、bridge、安全验证和场景文档，Codex 复核并退回一次具体 delta（临时数据 API、根场景依赖、原子发布、上下文回滚等）；完成会话已归入 MCP workspace。Codex 负责启动修补、集成、真实验收和最终文档。无提交/推送。
- 新增 create_case_scene/switch_scene/list_scenes/save_scene/inspect_blend_file 五个具名工具（服务端共 47 工具，bridge 共 43 命令）。旧 save_blend 整文件保存语义保持不变。新场景不复制/删除旧数据，集合链接拒绝其它场景或跨场景共享集合。
- 单场景导出使用 libraries.write 的必要依赖闭包；拒绝其它场景/外部对象/集合与 linked/override 数据。Project 内唯一暂存文件先只读验证，再原子无覆盖发布；主工程路径/场景不变。外部素材保持绝对引用而未打包，metadata 不单独枚举 mesh-owned shape keys，限制明确写入文档。
- 验证：16 项启动回归、22 项场景单元/schema、71 项真实官方 MCP stdio 检查全部通过；8 项补充工作流核对通过。修正两个验收断言以匹配既有 create_node_group 的 group.name 和实际可链接 ID 清单，未削弱其它案例内容检查。
- 独立文件：Project/算法复现/00_算法基础/_MCP验收_mcpiso_ab8e66ae/isolated.blend、next_case.blend（压缩，62920 bytes）、recovered.blend（164400 bytes）。第二案例只有自己的场景/对象，故意跨案例引用被拒绝且没有发布；移除该驱动器后恢复保存成功。验收后返回原 Scene，所有原对象详情、原节点图、场景记录和主工程路径相同。
- Project/chairs 与 Project/models 下 4 个已有 .blend SHA256 前后相同。没有打开、修改、覆盖或删除这些文件；没有开始 31 篇案例复现。验证创建的临时案例场景和文件保留供审阅。
- 当前 GUI PID 94804、Blender 5.2.1 LTS、127.0.0.1:9876；原生当前会话 health/ping/list_commands 再次成功，桥已持续在线。新命令均有审计，检查发现 scene 命令未关联请求号数量为 0。日志/报告/哈希证据位于 MCP/.runtime，不入库。
- 新增 schema 需 Codex 刷新/重启服务后显示；现有 GUI bridge 已加载新命令，重启 Codex无需主动关闭它。启动/重连步骤在 docs/CONNECTION_DIAGNOSIS.md；参数、调用顺序和隔离限制在 docs/SCENE_ISOLATION.md。
- 修改文件：README.md；blender/bootstrap.py、blender/blender_mcp_addon.py；blender_mcp/tools/{__init__,launcher,meta,scene,_startup}.py；blender_mcp/launch_cli.py；launch_blender.ps1；tests/test_connection_startup.py、test_scene_isolation.py、test_case_workflow_acceptance.py；docs/CONNECTION_DIAGNOSIS.md、SCENE_ISOLATION.md、DEVELOPMENT_LOG.md。所有源码/Git/文档均在 MCP。
- 最终加强验收节点图，加入输入 Geometry → Transform Geometry → 输出 Geometry，并以 Offset=[0.25,0,0] 证明真实求值几何有 4 个顶点且 X 位移正确；使用入库完整工作流脚本再验收，74 项真实 MCP 检查和 8 项原状态/后续案例/恢复检查全部通过。16 项启动与 22 项场景单元仍通过。
- 最终样例更新为 Project/算法复现/00_算法基础/_MCP验收_mcpiso_0376438a/isolated.blend（169897 bytes）、next_case.blend（62913 bytes）、recovered.blend（170073 bytes）。目录内恰好 3 个文件，无暂存或备份残留；已有 4 个模型文件 SHA256 再次一致。较早验收产物作为运行记录保留，不作为 31 篇算法案例复现。

## 2026-10-04（插件页缺失反馈复核）

- 针对用户仅显示 Dsh 的截图，重新核对实际安装记录：`codex plugin list --json` 的 installed 列表中 Blender 插件 installed/enabled 均为 true，标识为 blender-mcp@blender-local；记录位于 .runtime/codex_plugin_recheck_20261004.json。
- 本聊天原生 blender_health 调用成功，确认 Blender 5.2.1 LTS、127.0.0.1:9876、GUI PID 94804。当前聊天有 42 个 Blender 工具。未使用电脑操作，未改模型或重复安装插件；插件页是否已刷新仍需用户界面确认。新增 5 个工具需刷新/重启 Codex。
- 更新 CODEX_PLUGIN.md 和本日志；未提交或推送。

## 2026-10-04（原生 Frame 与独立节点注释扩展）

- 调查发现：通用 create_node 能建 NodeFrame，set_node_property 部分支持标题/颜色/shrink，但 parent 和 Text 指针不在安全白名单；缺少归组/解除、关联正文、布局摘要和独立插件能力探测。没有把原生 API 存在误当成 MCP 接口已实现。能力矩阵、完整工具参数和边界见 NODE_FRAMES_AND_NOTES.md。
- 增加 7 个 blender_node_frame_* 工具与 6 个 blender_node_note_* 可选工具，服务源码共 60 工具、bridge 共 56 命令；旧 47 工具 input/output schema 与默认值保持。原生操作、可选插件适配、独立 Extension 分层，MCP server 不 import bpy 或插件。所有新增桥命令复用原有审计和错误通道。
- 原生 Frame 精确节点组/节点名定位；全部目标预校验，默认不自动建 Frame。归组/解除保留嵌套画布位置、宽度、端口与计算连线，重复调用幂等。不自动重排旧图、不删除 Frame/Text、不默认覆盖普通用户正文，空正文需明确允许。新增稳定内部 Text 与所有权/共享保护；失败恢复如实报告。
- 独立扩展源码仅在 Mod/blender_mcp_node_notes/repository/blender_mcp_node_notes，manifest 0.1.0、最低 Blender 5.2.0、API_VERSION=1。Node Annotator 仅参考公开体验，没有依赖、复制或安装其源码。插件不依赖 MCP，可独立加载/使用，注释存入原生 Frame.text + Text + UUID。
- 委派 dsh 实现限定 Mod 文件集，Codex 负责架构、原生 MCP、适配器、集成和真实验证；两次在同一会话退回具体修复：真实 RNA 属性注册、删除补偿和精确正文。完成会话已归入工作区，无提交/推送。Codex 追加默认 fake user 处理、正文可见字号/边框，以及独立实机测试。
- 实际程序 reports Blender 5.2.1 LTS；扩展通过 preferences.extensions.repos + addon_utils 从 Mod 固定本地仓库加载，未复制到 MCP 包/Version。受控偏好 MCP/.runtime/blender_config，官方 BLENDER_USER_EXTENSIONS 指向 Mod 插件自己的 .runtime/blender_extensions；环境变量经官方资料与实际 --help 核实。persist=true 仅允许项目内 CONFIG；其它路径拒绝持久写入。
- 面板 MCP Notes 提供 Load/Apply、明确目标与 UUID 保护；具名 create/update/delete/open_text operators，多行正文复用现有 Text Editor，不开临时窗口。普通用户 Frame 不被插件接管；每个注释独立 Text，重复编辑同一数据块，共享正文拒绝。删除须明确确认，默认保留 Text，明确清理才删除自己的无其它真实引用文本。
- 本机定位并修复：TEXTEDIT_UPDATE 不是 StringProperty subtype；future annotations 导致 RNA 属性字符串化；Text.as_string 不追加换行；Text 默认 fake user 不能误当共享用户；Frame 默认字号 0 让正文数据有效但不可见。新注释字号 24、关闭 shrink、边框 520×220，实际三行正文可见。
- 执行命令（MCP cwd）：.venv/Scripts/python.exe tests/test_node_frames.py（3 通过）、tests/test_node_frames_atomic.py（9 通过）、tests/test_node_frames.py --live（最终临时禁用插件下 20 通过并恢复启用；已启用时为 18 项，另两项仅适用于缺失状态）；../Mod/blender_mcp_node_notes/tests/test_notes_stub.py（53 通过）、test_mcp_adapter.py（12 通过）、run_validation.py（manifest 成功、108 API 检查、21 GUI 检查均通过）。既有 58 验证、22 场景、16 启动检查亦通过。没有无条件重复整个套件。
- run_validation.py 有 60/120/120 秒子进程上限，输出仅在 Mod 插件 .runtime。真实 API 检查覆盖 RNA、独立/共享 Text、空正文、删除子节点位置/宽度/连线、明确清理和保存重开。GUI 注入一次真实部分文本写失败，正文/标题恢复；stub 补充删除/创建故障与恢复失败诚实报告。
- GUI 像素证明：Mod 插件 .runtime/canvas_18891dbb/node_canvas.png（1178×726）与 node_sidebar.png 均实际查看，三行正文和 MCP Notes 面板可见；Input → Process → Output 完整入镜、无正文遮挡。通过 POST_PIXEL draw callback 读取实际 GPU framebuffer，再 Image.save；早期 timer/screenshot 产生 1×1 的文件未计为有效证据。没有 computer-use、桌面抓屏或模拟键鼠。
- 保存关闭重开仅使用独有验证 fixture；roundtrip.blend、verified.blend 与 report.json 在上述 Mod 目录。先前桥源码重载前新建检查点 Project/demo/frame_notes_checkpoint_20261004_11677a.blend；只停止本任务拥有且已保存/空白的实例。未停止用户其它 Blender。Project 原有 4 个模型 SHA256 前后一致（frame_goal_asset_hashes.json），未打开、修改、覆盖或删除。
- 最终运行 GUI PID 124124、127.0.0.1:9876、非后台；当前聊天原生 health 再次成功，56 命令齐全。新增 Frame/Note 审计 156 事件，缺 request_id 为 0。报告 MCP/.runtime/frame_goal_final_review.json 与 Mod/.runtime/{validation_summary.json,mcp_adapter.json}，runtime 不入库。17 个相关 Python 文件语法检查、Git diff --check 通过，只有既有行尾提示。
- 当前聊天工具缓存仍为 42；新 60 工具 schema 已经官方 stdio SDK 实际列出和调用，但需刷新/重启 Codex 才成为本聊天原生工具。不需要重复安装插件，现有 GUI bridge/独立扩展已启用。
- 限制：目标仅 bpy.data.node_groups，其它节点树只用于共享审计；原生正文 16,384 字符，插件正文 10,000 字符/200 行；长正文需调整 Frame 或缩短。确认弹窗未人工点击，明确确认与拒绝路径已实测；其它 Blender 版本/平台未实测。删除补偿可能重建 RNA 指针，逻辑身份恢复不等于指针原样，恢复失败明确 rolled_back=false。
- 本次新增 MCP 文件：blender/node_frames.py、node_notes_adapter.py；blender_mcp/tools/node_frames.py、node_notes.py；tests/test_node_frames.py、test_node_frames_atomic.py；docs/NODE_FRAMES_AND_NOTES.md。修改 README.md、blender/blender_mcp_addon.py、blender_mcp/tools/__init__.py、blender_mcp/launch_cli.py、docs/DEVELOPMENT_LOG.md。
- Mod 文件：repository/blender_mcp_node_notes/{__init__.py,notes.py,operators.py,ui.py,state.py,blender_manifest.toml}；README.md、.gitignore；tests/{README.md,stub_bpy.py,test_notes_stub.py,test_blender_notes.py,test_mcp_adapter.py,verify_canvas.py,run_validation.py}。经验文档：经验文档/Blender_Frame_节点注释_经验.md。MCP Git 与文档保持在 MCP，插件专属源/文档/验证在 Mod；无提交或推送。

## 2026-10-04（节点注释插件中文使用说明）

- 按用户要求新增经验文档/Blender节点注释插件使用方法.md，并更新该目录 README 索引。涵盖启用、侧栏 Load/Apply、现有 Text Editor 多行编辑、具名 MCP 参数示例、Frame 归组、明确确认删除、工程保存和常见问题。
- 说明 Text Editor 直接修改正文后应 Reload 侧栏，避免旧缓存覆盖；区分插件操作的共享保护与原生手动编辑。明确确认删除已实测，侧栏确认弹窗未人工验收，遇到 confirmation_required 使用具名 MCP confirm=true。
- 根据当前插件源码与既有实测记录核对；仅修改文档，没有启动 Blender、修改模型或重跑运行测试。未提交或推送。

## 2026-10-04（现有工程打开命令：源码集成，真实加载待验证）

- 当前原生 MCP 连接正常：Blender 5.2.1 LTS、GUI PID 120772、127.0.0.1:9876。health/ping/list_commands 已验证；当前运行 bridge 仍为 56 命令，未加载 open_blend。不关闭用户实例、不另开实例、不输出 token。
- dsh 在限定的四个文件中实现文件打开桥模块、类型化 MCP 模块、针对性测试与文档；Codex 复核并在同一会话退回路径、状态指纹、失联证据和超时修复。Codex 完成主 addon/server 接线、入口刷新、验收脚本及文档集成。无提交或推送。
- 新增 blender_open_blend(path, confirm_replace_current=false, load_ui=false)：只允许 Project 内规范化绝对现有 .blend，始终要求明确确认替换；正式 RNA 检查后调用 wm.open_mainfile，固定 use_scripts=false。无隐式保存、无目标写入、无额外进程。源码官方 SDK 工具清单为 61。
- 打开后只读核验当前路径、同一 PID、health 和命令清单；丢失回复时只根据本次 bridge 审计及加载状态核验，不重放打开。证据不足返回 load_pending/verification_failed。路径、预检或加载错误可区分，不伪报回滚。
- bootstrap 新增独立 file_open 模块刷新，复用当前监听与计时器；其余命令、工程与 token 不变。需要用户在当前 Blender 从磁盘打开并运行新版 bootstrap 后，才能进行真实加载验收。用户询问用途，已说明；尚未收到运行完成回复，未假定已更新。
- 已实际验证：58 项 file_open 单元检查、1 项入口刷新检查、3 项既有 schema 检查、16 项连接启动检查、22 项场景单元检查、30 项审计检查通过。真实场景隔离 74 项检查及 8 项原场景/恢复工作流检查通过；4 个原有模型 SHA256 均未改变。Python 语法检查及 git diff --check 通过。
- 委派执行器额外运行旧 test_unit_validation.py 时有 3 项 setup 权限错误（测试夹具路径超出该运行时写根）；已要求停止该无关套件，未修改权限或绕过拒绝。这不是新增 file_open 的 58 项测试结果。
- tests/test_open_blend_acceptance.py 已准备但未运行；真实打开、load_ui 两种模式与加载后 bridge 存续尚未验收，不标记目标完成。后续只使用独有验证文件，并以单独具名保存创建会话检查点保护当前数据。

## 2026-10-04（手动入口重复注册报错与首次真实打开验证）

- 用户运行 bootstrap 后报告 ValueError: register_class already registered。原入口在注册新文件命令后再次调用 addon.register；旧 register 只捕获 RuntimeError，未处理此 ValueError。原生 health/list_commands 证明同一 PID 120772 仍在线且 open_blend 已注册（57 命令），工程未被该入口替换。
- bootstrap 改为先探测健康 bridge 并返回复用，仅需要启动时才注册 UI 类。未捕获并吞掉其它 ValueError，也未停止/重载整个 addon。现有入口刷新回归加入 duplicate-register ValueError 注入，证明健康连接分支不调用 register/start/stop；1 项入口与 16 项启动检查通过。
- 真实官方 SDK 经具名 MCP 成功打开专用 scene-only fixture，当前文件路径正确、同一 PID、bridge/命令清单存续；默认未确认、非法路径、缺文件与损坏容器均拒绝。修改 Transform Geometry 的 Translation 后实际三角形 X 范围由 [0,1] 变为 [0.25,1.25]，原目标文件哈希未变。
- 验收脚本修正实际返回字段 mesh；创建图时 with_input_output=false，避免自动默认输出与新输出重复，使验证图真正产生三角形。对各原场景分别激活后比较完整 object_info（含几何），避免跨场景依赖图的缓存值导致伪差异，未删除几何断言。失败过程的报告保留为 first_run/second_run，第三次报告保留实际节点编辑与恢复证据。
- 真实重开当前同一路径暴露 bpy.data.temp_data + libraries.load 仍拒绝当前文件的问题。新增仅此路径使用磁盘字节副本作预检，副本在 MCP/.runtime，枚举后删除；源文件变化拒绝，不保存当前 Main，不写目标。60 项 file_open 检查通过，包括副本解析与错误清理，尚待用户再次从磁盘加载入口后验证该分支。
- 最新恢复检查点：Project/算法复现/00_算法基础/_MCP打开验收_OpenVerify_23911c89/session_checkpoint.blend。原场景 Scene 激活，所有验证开始时的原场景对象完整属性与节点图恢复一致，4 个既有模型文件未改变。当前路径是明确新建的检查点，并非原先未保存的空路径。
- 当前未宣称完整验收通过；已请求用户重新打开磁盘新版 bootstrap 后运行，继续同实例验证，无第二个 Blender、无关闭、无任意代码/原始 socket、无提交或推送。

## 2026-10-04（主线程自我探测修补与最终打开验收）

- 用户再次运行报告 line 165 注册错误。定位真实原因：bootstrap 和 bridge timer 同在 Blender 主线程；阻塞式 TCP 探测自身时，计时器不能响应，超时后落入 register。入口现在直接核对并复用 addon._SERVER 的运行状态、地址及进程内凭据；配置不符拒绝且不停止/启动，本进程无运行服务器时才探测其它实例。凭据仅内部比对，未输出或记录。
- tests/test_file_bootstrap.py 三项通过，新增禁止自我探测及配置不符检查。该最后入口修补未再要求用户人工运行；文件打开工具已加载，与入口分支模拟验证分开记录。
- 用户此前再次运行虽在入口末尾报错，已刷新 file_open 模块。随后原实例 PID 120772 的官方 SDK 具名 MCP 验收 28 项全部通过：load_ui=false 打开、load_ui=true 重开当前文件（temporary_disk_copy 预检）、节点读写及实际几何变化、健康与清单存续、未确认/非法路径/损坏容器拒绝、只读检查、目标及检查点哈希不变、全部原场景对象完整属性及节点图恢复。
- 最终场景 Scene，检查点 Project/算法复现/00_算法基础/_MCP打开验收_OpenVerify_8ad6d5e1/session_checkpoint.blend；使用独有新增文件，不覆盖原工程。4 个原有模型与最初 SHA256 基线一致。测试报告 .runtime/open_blend_acceptance.json；先前失败记录保留，未作为成功证据。
- 实际 bridge 为 57 命令、source MCP 为 61 工具。MCP 下测试：tests/test_file_open.py（60 通过）、tests/test_file_bootstrap.py（3 通过）、tests/test_connection_startup.py（16 通过）、tests/test_open_blend_acceptance.py（28 真实检查通过）；场景隔离保留 74 真实检查与 8 工作流核对证据，不重复整个套件。
- 文档按原九条验收更新；后台模式、超大文件加载、强行断连接未做破坏性实测，相关故障用单元模拟覆盖。用户要求聚焦已保存工程打开，核心功能已实际验证，不再反复要求用户运行脚本参与验收。无提交或推送。

## 2026-10-05（Outliner 与显式工具域接入，进行中）

- 实际 health/命令清单、scene_summary/list_scenes 与源代码形成 Collection 能力矩阵；程序磁盘路径为 Version/5_2_1_LTS/blender.exe，运行版本为 5.2.1 LTS。原 MCP 61 工具/桥 57 命令；已有工具不能代替集合层级和已有对象成员编辑。
- 新增独立 outliner bridge/MCP 模块与六个基础具名工具；全路径预检后才创建，整批对象先验证，link 保留其他归属、move 只移除显式来源、unlink 不删除对象并保护最后场景归属；DAG/共享场景/多父写保护。审查修复移除后访问失效 RNA 的错误，并用模拟失效数据覆盖。
- 按操作域注册并用官方 SDK 1.30.0 FastMCP.remove_tool 做启动时过滤。原环境不设 profile 继续 all；新示例显式 core/outliner/geometry_nodes/algorithm_reproduction。Outliner 中去除 present_object（3D View），list_objects 属于 core。
- 真实 server.py 五个 stdio 实例的 SDK tools/list 和禁用工具直接 tools/call 通过：基础接入数量为 all67/core19/outliner25/GN35/algorithm41；原61 input/output schema不变；未知profile以明确配置错误退出，无协议stdout污染。报告 .runtime/outliner_profiles_integration/report.json。后续增加管理工具须重测，不能把该数量称为最终清单。
- 用户追加 Collection/对象的创建、删除、移动、重命名、Hide/Unhide、Disable Render/恢复等要求；截图中的“实例”实际是 Object/Mesh Object，已明确需要普通对象管理，集合实例专用实现需调整。显示状态区分 LayerCollection/Object 的眼睛、数据块的视口禁用和渲染禁用。永久删除需明确确认、保留非目标数据并诚实报告不可回滚限制。
- 核心旧工具与入口模拟 66 项通过；基础Outliner与入口56项通过；profile实现43项通过（实现方），Codex另用真实服务入口交叉验证。当前Blender仍57桥命令，新工具尚未实机加载/验证，进度详见 docs/OUTLINER_ACCEPTANCE_STATUS.md。
- chairs/models四原资产哈希仍与基线一致；算法总工程被外部活动更新，本任务未保存该工程，不能宣称相对早期基线未变。后续实机验证要取即时基线且不影响活动工程。
- 经验文档/算法/复现案例总提示词同步增加 Outliner/工具集/Frame预览预检及Object术语。所有MCP源码、Git、文档、日志和测试均在MCP目录；无提交、无推送、无桌面自动化、无原始socket、无既有模型修改。

### 2026-10-05 最终源码与服务清单复核

- 用户截图明确“实例”为普通 Object/Mesh Object；已去除集合实例专用创建接口，改为普通 Mesh/Empty 创建、重命名、删除。Object 移动复用显式来源成员接口，不改三维位置；集合和对象显示区分眼睛、监视器、渲染，集合另支持视图层排除。
- 两个 bridge/MCP Outliner 模块已接入 addon、bootstrap 和 domain 注册。路径、整批成员和永久删除均先预检；补齐共享祖先写保护、完整场景归属检查、未知引用状态保守拒绝；永久删除失败如实报告残留，保留非目标底层数据。
- 针对性回归命令 `python -m unittest tests.test_outliner tests.test_outliner_management tests.test_file_bootstrap tests.test_file_open tests.test_node_frames tests.test_connection_startup tests.test_tool_profiles`：261 项通过。
- 正式入口 `python tests/test_outliner_profiles_integration.py`：五个实际 server.py stdio 进程，官方 ClientSession 查询清单和禁用工具调用，通过；最终 all76/core19/outliner34/GN35/algorithm50。旧61 input/output schema不变，未知profile退出2，无Traceback/stdout污染。报告 .runtime/outliner_profiles_integration/report.json。
- tests/run_all_tests.ps1 无 Blender 单元阶段已接入新回归和正式 profile 检查；实机脚本仍单独手动门禁运行，不启动额外 Blender 或自动修改活动工程。
- 当前 PID131628/5.2.1LTS 仍57旧桥命令。已请求一次从磁盘刷新 bootstrap，尚待用户结果。不能把正式 MCP 清单通过写成 Blender 实机通过；未创建新的实机夹具，未调用用户工程保存或对象编辑。

### 2026-10-05 门禁与交付要求复核

- 重新读取目标并用原生 health/list_commands/list_scenes 核对当前实例，仍57命令、2场景/6对象，缺少15个Outliner命令。运行实机脚本门禁实际退出2，fixture_created=false、checks=[]、saved_blend=false；未创建数据，等待已请求的磁盘入口刷新。
- 实机报告现在保留PID/版本、缺失清单、独有夹具名、已完成检查及是否创建，失败不丢弃定位信息，也不复用旧报告。显示逐开关读回、夹具层级比对之外，新增非空GN连线快照前后比较；该新增原生验证仍待实际执行。
- 四原模型哈希再次与初始基线一致；算法总工程与初始基线不同，不宣称整个文件未变。最新记录 .runtime/outliner_hashes_recheck.json。本任务未调用用户工程保存或对象编辑。
- OUTLINER_ACCEPTANCE_STATUS.md 按目标章节及用户追加范围逐项列证据；源码/SDK通过与原生运行缺失分开，目标仍未完成。

### 2026-10-05 首次Outliner实机运行与删除引用误判修复

- 用户执行bootstrap后原生health确认同一PID131628、72桥命令、全部15个Outliner命令。当前Scene为用户切换的Alg00_坐标重映射与点构造，MCP测试保持该Scene和当前filepath，不保存。
- 测试调用辅助函数参数名与工具的name字段冲突，在创建Scene前报TypeError；改为tool_name后进入真实测试。27项检查通过，包括三级层级/幂等、成员link/move/unlink、GN非空连线快照、错误零副作用、集合4个及对象3个显示开关True/False读回与普通对象改名。
- 普通对象删除被拒：Blender user_map实际包含所属Scene（结构性ID引用），Object.users仍等于Collection数量。原判断把Scene误当成外部依赖，未删除任何对象。失败报告 .runtime/outliner_live_after_refresh_failed.json；独有未激活临时Scene MCPOutliner_f25ccb0d_Scene及对象留在内存，未保存。
- 修复仅排除完整归属和用户计数证明的结构性Scene；Scene.camera、timeline marker、驱动变量及keying-set实际引用仍受保护，非所属Scene或额外users仍拒绝。删除整批安全检查读取完整归属而非紧凑报告。增加5项针对性回归；整体266项通过。
- 已请求用户再运行同一bootstrap加载修复后完成最终删除/递归保护验收，不把27项局部实测写成完整通过。


### 2026-10-05 参数化检查、Reroute及维护入口

- 扩展既有FastMCP 1.30.0 domain/profile机制，新增13个检查工具与1个固定模块维护工具；默认兼容all90，algorithm_review精确26，无任意Python/rawsocket/桌面自动化。
- 实例Properties整批预检、类型/范围/引用/属性模式校验与输入快照回滚；模型检查精确Scene/ViewLayer context override和临时mesh清理；原生Reroute保持多输入排序与数据流。
- 实际Blender 5.2.1 LTS/PID127096：独有Project/MCP验收夹具，Columns4→6、mesh24→36顶点，共享默认/另一实例保持4；错误批次无部分更新。单link/分支/multi-input插入、移动、删除及几何signature/端点顺序通过。
- GUI截图实測暴露RNA wrapper identity比较和imbuf.free错误；改为as_pointer与ImBuf.free，失败路径恢复UI并拒绝错误/陈旧图像，新增针对性测试。真实图像回传尚待最终刷新验证，不伪报已通过。
- dsh三工作流遇provider QUOTA，Codex完成已授权整合与修复。未绕过执行器拒绝写入的geometry测试文件，该文件现有53项测试通过。
- 组合离线214项通过；实际stdio profiles90/26/20/35/42/57，旧76schema一致；旧Outliner正式协议通过旧61schema一致。git diff --check通过，未commit/push。
- 用户指出反复手动bootstrap不便，补无参数reload_review_commands/blender_reload_review_bridge：只刷新5个固定本地模块、核对来源、保留listener/token/工程，初次需要bootstrap装入。源码及协议已验证，当前运行时未装入不冒充成功。
- 保护核对：总工程磁盘hash和36对象一致；30/31主GN完整一致，另一组4节点横坐标±1差异，计算图/参数/引用一致。保留差异报告，不覆盖用户现有布局；最终保护结论仍需注明该差异。

### 2026-10-05 Field、拓扑回滚与编辑器恢复复核

- 当前 Blender health 仍为 PID127096/5.2.1LTS，维护命令未加载。用户询问为何不能自己运行 bootstrap：现有 typed bridge 不具备脚本/维护执行入口；首次加载固定维护工具后才可远程刷新。没有通过任意 Python、raw socket 或桌面自动化绕过该边界，也没有重复请求用户同一操作。
- 对照 v5.2.1 官方生成 RNA/socket 源码，属性名与 type 枚举是独立状态。修复单写属性名误判模式、补枚举现场读取和显式模式开关、读回校验、动态隐藏输入拒绝、点号驱动路径；模式数值无硬编码。快照读取失败预先拒绝；恢复值也读回验证，未完成恢复不声称“无变更保留”。
- Reroute 插入/删除传播静音连线，重排前缓存旧 link 静音避免访问已删除 RNA。签名增加有效静音等价；回滚核对节点/显示状态、链接与 multi-input 顺序，失败报告 rolled_back=false。
- 编辑器捕获始终 pin 指定目标，检查 edit_tree 指针；在目标 area override 中切换/恢复。原嵌套路径与同名材质 Shader 树按指针恢复；先恢复 tree_type 再赋树，符合官方 RNA 类型 poll。必要布局/模型证据失败则不发布 PNG，恢复 UI。
- 新增独立 test_properties_modes.py、test_geometry_nodes_edit_safety.py、test_geometry_nodes_capture_safety.py 共16项安全验证；未修改被执行器拒绝的现有 geometry 编辑测试文件。回归入口已接入新模块。组合230项通过；本轮修改属于离线验证，实机结果仍待首次维护入口安装。
- 所有源码/文档/日志保持在 MCP 目录；无 commit/push、无用户工程保存或原案例数据编辑。

### 2026-10-05 动态菜单枚举与最终验收门禁

- v5.2.1官方 node_socket.cc 表明菜单 item.value 是运行时 item identifier，而非列表序号。参数模块优先读取实例生成RNA的value枚举，保留identifier/name/真实value；支持identifier映射到整数、非连续整数值，数值映射无法证明时拒绝。新增3项非连续枚举/缺失数值映射测试。
- viewport捕获元数据增加截图时着色方式、颜色模式、透视、距离、位置和旋转；MCP图像说明使用实际capture operator，不再一律写screen.screenshot_area。
- 最新组合233项通过；正式server.py stdio再次通过all90/review26/core20/outliner35/GN42/algorithm57，旧76schema不变。当前安装包mcp1.30.0/pydantic2.13.5。
- 续验脚本先检查固定维护命令，再刷新固定模块并核对PID/路径/场景。图片验收比较收到的ImageContent字节、发布PNG和SHA256，并核对真实绘制、精准树、UI恢复；增加SELECTED/sidebar和有实际数值变化的复合迭代，保留另一实例/共享默认检查。仍复用原夹具。
- 现场门禁实际返回bridge_refresh_required，缺少reload_review_commands，test_mutations_performed=false、saved_blend=false；无新夹具、无参数/拓扑编辑。没有启动额外Blender或绕过具名bridge。逐项目标证据/缺口已记录在验收状态表中，不能因静态/协议通过宣称截图已验收。

### 2026-10-05 自动维护与实机验收完成

- 外部状态变化：旧 Blender 已关闭。经具名 blender_launch_blender 启动自动装入 bootstrap，实际5.2.1LTS/PID151944、9876；具名 open_blend 打开既有MCPReview_f4ddb108/fixture.blend。固定维护命令已装入并多次验证保留PID/工程/Scene，不再要求用户手动刷新固定模块。
- 非 asset GN 树直接 node_tree 赋值受官方 poll 限制，改用原生 space.path.start 定位，不更改组Usage。默认使用最大区域，初次绘制后取景，等待平滑导航结束后的真正绘制，修复窄区/sidebar遮挡；取消的fit报告错误。实际2100×1193原生Window图片回传，ImageContent字节/发布PNG/SHA256一致，AI检查视口及ALL/Frame SELECTED/sidebar图。
- 真实向量 IDPropertyArray 不能与 list 直接比较，快照需复制数组；修复并加回归。实例枚举不能先list化保留失效RNA条目，改逐条读取，真实正实例=1、9点、包围盒及清理通过；非Mesh实例/失败的边界明确不完整。参数求值场景也使用RNA指针身份。
- 现有创建接口工具省略default时错误注入null，导致菜单创建失败；仅保留显式参数，省略值由Blender提供，明确null数据块默认仍支持，旧schema不变。
- 实机整型Columns4→6/顶点24→36；Field position尺寸1→2、VALUE Z=1；现场菜单0/1宽度1→3；Float/Bool/String/Color/Rotation/Object/Collection读回及Material空值/错误类型；错误identifier、对象/Modifier、隐藏、类型、引用及批次无部分更新。另一实例与共享接口默认不变。
- Reroute单link/共享分支/独立多输入完整复测：counts/bounds/完整SHA256及端点/排序等价；仅明确测试节点布局与Frame，实际截图可读。扩展测试图单独save_scene(overwrite=false)导出Project/MCP验收/.../extended_fixture_instances.blend；不覆盖原夹具或案例总工程。
- 正式stdio profile清单all90/review26/core20/outliner35/GN42/algorithm57，旧76schema一致；旧Outliner基线61schema一致。静态、离线、正式协议与实机证据分开保存，所有代码/Git/日志/文档仍在MCP，未commit/push。
- 原算法总工程完整文件SHA256保持初始值；当前只加载专用测试工程。历史旧进程36对象/30主GN一致、另一主GN4处±1布局差异未归因，旧会话已关闭，不能伪称最终未保存内存快照全部一致。Material/Image非空、内部GeometrySet所有组件等未覆盖范围明确写入验收状态。
- 最终增强/相关模块组合243项通过；旧相关模块293项分别在独立进程通过。混合导入时3个no-bpy断言受到其他测试fake bpy污染失败，未修改断言；独立运行与正式stdio实测通过。旧测试退出ResourceWarning保留在本地日志；git diff --check无错误，仅换行提示。
