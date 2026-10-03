# 项目开发日志

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
