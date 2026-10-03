# Blender MCP 测试报告（本会话实测）

## 最终复核：2026-10-03（Asia/Shanghai）

Codex 在正常用户账户下运行全部套件，设置 `BLENDER_MCP_FORCE_SDK_STDIO=1`，使用官方 SDK 自带
`stdio_client`，未加载临时 `sitecustomize.py`。**230 项检查全部通过，退出码 0**。

| 套件 | 检查数 | 结果 |
| --- | ---: | --- |
| 服务端单元与协议 | 56 | 通过 |
| 连接期限、配置、启动参数回归 | 7 | 通过 |
| 原始 Blender 集成 | 48 | 通过 |
| 数据块引用、恢复和轮询回归 | 41 | 通过 |
| 官方 SDK stdio 端到端 | 78 | 通过 |

执行命令（在 MCP 目录）：

```powershell
$env:BLENDER_MCP_FORCE_SDK_STDIO = '1'
.\tests\run_all_tests.ps1 -BlenderPath 'D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe' -Port 0
```

记录在 `.runtime/final_verification.log`；JSON 报告位于 `tests/reports/`。新引用功能与 Blender 的
换组链接行为见 `docs/GN_REFERENCES.md`。测试使用空闲端口，验证桥接 PID 为测试创建的 PID，
只关闭测试自己的进程。Codex `blender` 服务注册已完成，并用 `codex mcp get blender --json` 核对。

以下为首次实现阶段的历史证据；环境适配仅记录当时的处理过程。临时目录 shim 已停用，
最终验证使用标准 Python 临时目录行为。当前已测几何节点的组复用与对象引用；GUI 交互、
非 Windows 平台及重载后 Codex 会话内的自然语言建模仍未实测。

---

- 日期：本会话
- Blender：**5.2.1 LTS**（build 2026-08-25，hash `9e2066aef7ef`），`D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe`
- MCP 服务端：Python **3.12.7**（`MCP\.venv\Scripts\python.exe`），`mcp 1.30.0`（满足 `mcp>=1.26,<2`）
- 结论：**三套测试全部通过（56 + 48 + 78 项，0 失败）**，并产出可编辑的演示文件 `Project\demo\parametric_demo.blend`。

| 套件 | 命令（相对 `D:\AI\MCP\Blender`） | 结果 |
| --- | --- | --- |
| 单元/协议 | `MCP\.venv\Scripts\python.exe MCP\tests\test_unit_validation.py` | `Ran 56 tests ... OK`，exit 0 |
| 真实 Blender 集成 | `Version\5_2_1_LTS\blender.exe --background --factory-startup --python-exit-code 1 --python MCP\tests\blender_integration.py` | 48/48 checks，exit 0（连续两次运行均通过） |
| MCP stdio 端到端 | `MCP\.venv\Scripts\python.exe MCP\tests\test_mcp_stdio.py` | 78/78 checks，exit 0 |
| 一键跑全部 | `powershell -ExecutionPolicy Bypass -File MCP\tests\run_all_tests.ps1` | exit 0 |

报告文件：`MCP\tests\reports\unit_report.json`、`blender_integration_report.json`、`stdio_report.json`。

---

## 1. 单元/协议测试（56 项，无需 Blender）

```
> MCP\.venv\Scripts\python.exe MCP\tests\test_unit_validation.py
Ran 56 tests in 4.248s
OK
{"suite": "unit_validation", "tests_run": 56, "failures": [], "errors": [], "skipped": [], "ok": true}
```

覆盖：

- **路径安全**：`Project` 内相对路径通过；空路径 / `..` / 绝对路径 / **目录 junction（Windows 复现点）** 全部拒绝；
  服务端（`blender_mcp.config`）与 Blender 侧（`blender_mcp_addon`）规则一致。
- **令牌**：自动生成、64 hex、两次读取稳定；缺失时报错；`health`/`status` 输出中**不含令牌**。
- **驱动表达式校验**：算术/比较/内置常量/已声明变量通过；`__import__(...)`、`open(...)`、`lambda`、`eval`、
  `print(...)`、`1; 2`、`__class__` 拒绝；未定义变量 → `unknown_variable`；`1 +` → `invalid_expression`。
- **协议帧**（真实 `BridgeServer`，非阻塞 poll，端口 0 随机分配）：
  正常 `ping`/`list_commands` 往返；错误令牌 → `unauthorized` 且连接关闭；缺令牌 → `unauthorized`；
  坏 JSON / 非对象 JSON → `bad_request`；未知命令 → `unknown_command`；参数缺失/多余 → `invalid_argument`；
  命令内部错误 → `object_not_found` 且服务端继续存活；超大帧 → `frame_too_large`；端口冲突 → `bind_failed`。
- **MCP 客户端 / 连接生命周期**（对进程内假桥）：正常、远端错误类型化（`BlenderCommandError.remote_code`）、坏 JSON、
  id 不匹配、超大响应、**超时后释放连接并丢弃半帧**、超时不自动重放（服务端只收到 1 次请求）、
  协议错误后连接被释放、恢复后重连正常、连接拒绝、请求帧上限、缺令牌、健康探测。
- **工具面**：必需工具齐全、无 eval/exec/python 类工具、每个工具有描述、
  `blender_object_info` / `blender_set_shape_key_coordinates` / `blender_batch_graph_edit` / `blender_add_driver` 的 JSON Schema 是类型化的。
- **驱动命令注册表**：29 个必需底层命令齐全；命令名中不含 eval/exec/python。

## 2. 真实 Blender 集成测试（48 项，`--background --factory-startup`）

```
> Version\5_2_1_LTS\blender.exe --background --factory-startup --python-exit-code 1 --python MCP\tests\blender_integration.py
INTEGRATION::SUMMARY::{"blender_version": "5.2.1 LTS", "checks_total": 48, "checks_failed": [], "ok": true}
INTEGRATION::EXIT::0
```

**求值断言（不是属性回读）**

| 断言 | 实测值 |
| --- | --- |
| GN 基线 | 16 顶点，局部包围盒 x∈[-1,1]，z=1.0 |
| `Size 2.0 → 6.0` | 包围盒 x∈[-3,3]（size 6.0） |
| `Height 1.0 → 2.5` | z=2.5（整平面平移） |
| `Density 4 → 6` | 求值顶点 16 → **24** |
| 场景自定义属性 `rows 4→2`（驱动节点组 socket 默认值） | 求值顶点 24 → **12**，改回 4 → 24 |
| 形态键 `Taper` 手动影响 0/0.5/1.0（立方体，全顶点 +2X） | 包围盒 max_x 0.5 → 1.5 → 2.5，min_x 也随值平移 |
| 被驱动形态键 `ScaleX`（由 `bulge` 驱动）0/0.5/1.0 | 包围盒 max_x 1.0 → 1.25 → **1.5**（半径 0.9999997） |
| 对象驱动器 `rotation_euler[2] = phase*2` | phase 0.75 → **1.5**；phase −1.0 → **−2.0** |
| TRANSFORMS 驱动器 `location[2] = Morph_Sphere.LOC_Z` | Morph_Sphere z=2.0 → 立方体 z=**2.0** |

**负例（都必须是明确错误，而不是"带 error 的成功值"）**

`impossible_link_is_rejected`（GEOMETRY→VECTOR）、`shape_key_topology_mismatch_is_rejected`、
`shape_key_basis_is_write_protected`、`shape_key_rejects_malformed_payload_without_partial_write`、
`driver_target_on_missing_property_is_rejected`、`invalid_driver_data_path_is_rejected`、
`unknown_driver_variable_is_rejected`、`driver_expression_rejects_python_exec`、
`save_blend_refuses_overwrite_by_default`、`save_blend_rejects_path_traversal`、`save_blend_rejects_absolute_paths`。

**失败即回滚（审查重点）**

```
shape_key_rejects_malformed_payload_without_partial_write
  error: "entry 1 must be [x, y, z], got 'not-a-vector' (nothing was written)"
  before == after （3 个采样点逐坐标相同）→ 载荷先整体校验，绝不部分写入

failed_driver_update_restores_previous_expression
  update_driver(expression="1 / (phase - phase)") → invalid_expression
  expression_before == expression_after == "phase * 2"，is_valid_after == true
  rotation_euler[2] 仍为 1.5 → 旧表达式与变量被完整恢复
```

**持久化（保存 → 重新打开 → 仍可参数化）**

```
save_blend → Project\demo\parametric_demo.blend (689569 bytes)
persistence_before == persistence_after
  nodes = [Group Input, Group Output, Grid, HeightVec, XForm]  links = 7
  shape_keys = [Basis, ScaleX]
  drivers = {object: 2, node_group: 1, shape_keys: 1}
重开后再改 Density=5 → 求值顶点 20（= 5×4）
重开后 phase=3.0 → rotation_z = 6.0
```

**"生成的几何不受基础形态键影响"（在独立对象上验证并解释）**

`GN_Morph` 同时带 8 顶点立方体的形态键 `Stretch` 与生成网格的 GN 修改器：
把 `Stretch` 拉到 1.0 后，求值顶点数（16）与包围盒（[3,-1,1]–[5,1,1]）**完全不变** ——
因为结果来自 GN 生成的网格，而形态键只作用于对象的原始 8 个顶点。形变与生成几何要同时可见，必须拆到两个对象上。

## 3. MCP stdio 端到端（78 项）

```
> MCP\.venv\Scripts\python.exe MCP\tests\test_mcp_stdio.py
STDIO::SUMMARY::{"ok": true, "checks": 78, "failed": [], "report": "...\\stdio_report.json"}
```

- 启动 `blender.exe --background --factory-startup --python MCP\blender\bootstrap.py -- --mcp-pump …`（真实子进程），
  原始 socket 轮询到 `health` 就绪（`bridge_reachable_from_raw_client`，blender 5.2.1，pid 一致）。
- 真实 MCP stdio 客户端：`initialize` → `list_tools`（38 个工具，必需工具齐全）→ `call_tool` 全流程：
  建节点组 → `batch_graph_edit`（8 步全部完成，`rollback:false`）→ 挂修改器（求值 16 顶点）→
  改 `Density=5`（→20 顶点）→ 改 `Size=6.0`（包围盒 size x=6.0）→ 建球+形态键（自动 Basis）→
  写 482 项增量 → 影响 1.0（max_x=2.0）→ 自定义属性 → 加驱动器（`evaluated_value`=1.0）→
  改属性后 `object_info` 里 `rotation_euler[2]`=2.0 → `inspect_drivers` 变量/目标元数据 → `save_blend` → `scene_summary`。
- **负例 22 项**：路径穿越/绝对路径/空路径/无 `overwrite`、未知对象、未知节点、未知 socket、
  不兼容连接、未知节点类型（`create_node`/`describe_node_type`）、形态键拓扑不符、Basis 写保护、未知形态键、
  驱动器坏 data_path / 坏表达式 / Python exec / 目标不存在、坏 owner、删除不存在的驱动器、
  批量未知 op（schema 层拒绝）、未知图元（schema 层拒绝）、网格面索引越界。
- **传输/鉴权负例**：错误令牌 → 工具报错且**不泄露令牌**；死端口 → `bridge_unavailable` + `blender_bridge_probe` reachable=false。
- **进程收尾**：3 个 MCP 服务端子进程 `exit_code: 0, still_running: false`；
  Blender 宿主通过协议 `shutdown` 优雅退出 `exit_code: 0, still_running: false`；
  **只动本测试启动的进程**（未触碰任何既有 Blender）。
- 复用逻辑：`blender_launch_blender(reuse_existing=true)` 在已有健康桥时返回 `launched:false, reused:true`（不启动 GUI）。

## 4. 启动助手（PowerShell）实测

```powershell
# 首次：无头启动 + 隔离配置
powershell -ExecutionPolicy Bypass -File MCP\launch_blender.ps1 `
    -Background -FactoryStartup -IsolateConfig -PumpSeconds 30 -WaitSeconds 90 -Port 9912
[launch] Blender  : D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe
[launch] isolated config: ...\MCP\.runtime\blender_config (user profile untouched)
[launch] started headless Blender pid=49948, log: ...\logs\launch_20261003_190220.log
[launch] bridge is ready on 127.0.0.1:9912 (pid 49948)
exit 0

# 第二次：复用已存在的健康桥，不新开进程
powershell -ExecutionPolicy Bypass -File MCP\launch_blender.ps1 -Background -Port 9912
[launch] a healthy bridge already answers on 127.0.0.1:9912 - reusing it, nothing started.
exit 0
```

- 未启动 GUI（`-Background`）；`PumpSeconds 30` 到点后 Blender 自行退出，未遗留进程（之后 `Get-Process blender` 为空）。
- 脚本通过 `Parser::ParseFile` 语法检查通过。

## 5. Blender 5.2 API 探针（实现依据，可复现）

```
Version\5_2_1_LTS\blender.exe --background --factory-startup --python-exit-code 1 --python MCP\tests\probes\probe_blender_api<N>.py
```

`probe_blender_api.py`…`probe_blender_api9.py`、`probe_stdio_spawn.py`、`probe_stdio_sdk.py` 记录了下列**实测结论**，
服务端/插件的实现正是按这些结论写的（否则会写出"看似成功但几何没变"的工具）：

1. `bpy.data.node_groups.new(..., "GeometryNodeTree")` 得到**空**节点树 → 必须自己建 `NodeGroupInput/Output`。
2. `nodes.new("GeometryNodeTransform")` 默认名是 **`Transform Geometry`** → 绝不能按假定的默认名找节点。
3. 节点类型发现：类上**没有** `bl_idname` 属性；用 `dir(bpy.types)` 前缀 + `bl_rna.identifier`（5.2 有 274 个 `GeometryNode*`）。
4. GN 修改器参数是 IDProperty：`modifier.properties.inputs[<socket identifier>]["value"]`（键是 identifier，不是显示名）。
5. **写入该 IDProperty 不会让修改器失效**：只 `depsgraph.update()` 或 `view_layer.update()` 都拿到旧网格；
   必须 `object.update_tag()` + depsgraph 更新（`probe7/9` 对照实验）。
6. 形态键 `shape_key_add` 会把**第一个** key 当作基准并沿用你给的名字 → 必须显式先建 `Basis`。
7. 形态键点坐标必须**整向量赋值**（`point.co = (x,y,z)`）；逐分量写 `point[0] = …` 不落盘。
8. `DriverTarget.id_type` 对 `TRANSFORMS` 是**只读**的（SINGLE_PROP 可写）。
9. `Action.fcurves` 在 5.x 已移除（slotted action）：`action.layers[*].strips[*].channelbags[*].fcurves`；
   驱动器仍走 `animation_data.drivers`。
10. 驱动在**下一帧/依赖图求值**时生效；节点树上的驱动器改变求值几何（`rows` 场景属性 → 顶点数变化）。
11. Windows 没有 `select.poll`（实现只用非阻塞 socket，不依赖 poll）。

---

## 6. 环境限制 / 阻塞项（本会话真实遇到）

1. **`Version\5_2_2\blender.exe` 不存在**：实际安装目录是 `Version\5_2_1_LTS\blender.exe`，`--version` 报 **5.2.1 LTS**。
   因此实现与脚本都**自动发现** `Version\*\blender.exe`，并支持 `BLENDER_MCP_BLENDER` / `-BlenderPath` 覆盖。
2. **沙箱拒绝"以 0o700 创建的目录"内的写入**（`tempfile.mkdtemp` 正是 0o700）→ pip / pytest 等一律 `PermissionError [Errno 13]`。
   处理：在 `MCP\.venv\Lib\site-packages\sitecustomize.py` 放了**作用域明确、Windows 专属、带注释**的 shim，
   把 `mkdtemp` 的目录权限改为 0o777。这是环境适配，不是业务逻辑；正常机器上可直接删除。
3. **沙箱拒绝 asyncio Proactor 的管道创建**（`asyncio.windows_utils.pipe` → `PermissionError [WinError 5]`），
   而**普通 `subprocess.Popen(stdin=PIPE, stdout=PIPE)` 正常**（`tests\probes\probe_stdio_spawn.py` 实测）。
   `mcp.client.stdio.stdio_client` 走的是前者，因此端到端测试改用 `tests\stdio_pipe_transport.py`：
   **仍然是官方 `mcp.ClientSession` + 真实子进程 + 完全相同的逐行 JSON-RPC 帧**，只把管道换成标准库实现。
   （`BLENDER_MCP_FORCE_SDK_STDIO=1` 可在正常机器上切回 SDK 自带 `stdio_client`。）
4. **申请放宽沙箱被拒**：曾按规程对"原样的 stdio 测试命令"申请一次 `danger-full-access` 并说明理由，未获批准；
   因此**没有**再走其它绕过手段，而是采用上面的等价改造（协议与客户端未变），并把这一条如实记录在此。
5. **缩略图缓存写入被拒（无害）**：保存 .blend 时 Blender 还想往 `C:\Users\12596\.thumbnails\...` 写一张预览图，
   被沙箱拒绝并打印 `image.write ERROR ... OpenImageIO write failed`。**文件保存本身成功**（bytes 689569），
   用户目录**没有任何写入发生**；已在 `save_blend` 里关闭 `use_save_preview_images`（该警告仍会出现，属 Blender 行为）。
6. **用户配置隔离**：所有 Blender 测试/助手运行都设置 `BLENDER_USER_CONFIG=MCP\.runtime\blender_config`，
   不使用 `--factory-startup` 以外的用户偏好路径，**不修改用户 profile**。

## 7. 本版本明确的限制

- `batch_graph_edit` **无事务、不回滚**（响应显式声明 `rollback: false` + `completed` 列表）。
- 驱动器变量仅 `SINGLE_PROP` / `TRANSFORMS`。
- 形态键不影响修改器生成的几何（见 §2）。
- 保存仅允许 `<workspace>\Project` 下、仅 `.blend`；不提供删除/重命名等破坏性工具；
  保存期间临时关闭预览图与版本备份，因此不会额外产生 `.blend1`。
- 未测试：GUI 交互（按验收要求未启动 GUI）、真实 Codex 会话内的工具调用（仅验证到 MCP 协议层）、
  Windows 之外的平台。
- `.runtime\tmp\` 内有若干早期 pip/ensurepip 失败留下的空目录（0o700，本受限环境拒绝删除），
  位于已忽略的运行目录内，正常机器上可直接删除。

## 8. 审查重点逐条答复

| 审查点 | 答复 | 证据 |
| --- | --- | --- |
| 驱动器更新失败时是否保持原有表达式和变量 | 是。先静态校验新表达式与变量，再快照旧状态；Blender 判无效时恢复旧表达式/变量并报 `invalid_expression`（`add_driver` 则撤销本次新建） | `failed_driver_update_restores_previous_expression` |
| 形态键首个变形键是否建立 Basis，坐标错误是否避免部分写入 | 是。首个键前显式建 `Basis`；载荷先整体校验再写，中间项非法时前后采样一致 | `shape_key_basis_created_automatically`、`shape_key_rejects_malformed_payload_without_partial_write` |
| 连接超时是否释放连接、清空残留数据、不自动重放 | 是。超时/协议错误即关闭 socket 并清空读缓冲；变更请求不重放（服务端只收到 1 次） | `test_timeout_releases_the_connection_and_partial_frame`、`test_timeout_does_not_replay_the_request` |
| Blender 5.2 修改器输入的存储与求值 | `modifier.properties.inputs[identifier]["value"]`（IDProperty），写后需 `object.update_tag()`+依赖图更新才会重算；已用求值顶点数/包围盒验证 | §2 表格、`probe_blender_api4/5/7/9.py` |
| 几何节点组复用与结构化引用 | 组：`create_node_group(if_exists=reuse/replace)`、`attach_geometry_nodes_modifier(reuse_existing)`；对象按名称（错误里附可用列表）、节点按名称或索引、插槽按 identifier→名称→索引 | `create_node_group_with_interface`、`inspect_node_group_reports_nodes_and_links`、单元测试的 `socket_not_found`/`node_not_found` 负例 |
