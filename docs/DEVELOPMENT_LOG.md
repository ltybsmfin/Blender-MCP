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
