# Blender MCP（Codex 用）— 几何节点 / 形态键 / 驱动器

> 架构约定见 `docs/ARCHITECTURE.md`，实测证据见 `tests/reports/TEST_REPORT.md`，开发日志见 `docs/DEVELOPMENT_LOG.md`。

在 **已运行的 Blender** 与 **Codex（MCP stdio 客户端）** 之间架一座本地、带令牌鉴权的桥。
第一版聚焦三件事：**几何节点参数化建模**、**形态键（Shape Keys）**、**驱动器（Drivers）**。

- MCP 服务端：官方 Python MCP SDK **1.30.0**（已固定版本）+ FastMCP stdio，**38 个工具**。
- Blender 侧：单文件插件/桥接脚本，**只用 Blender 自带标准库 + `bpy`**，不需要在 Blender 里安装 `mcp`。
- 传输：`127.0.0.1:9876` 上的 IPv4 TCP，逐行 JSON（`\n` 分帧），令牌写在 `MCP/.runtime/token`（两侧读取，**永不打印**）。
- **没有任意 Python 执行工具**：所有能力都是具名工具；唯一的表达式通道是 Blender 驱动器表达式，且会被解析（`ast.parse`）后按白名单校验，绝不 `eval`/`exec`。

---

## 1. 目录结构

```
<workspace>\MCP\
├─ server.py                     MCP stdio 入口（Codex 启动的就是它）
├─ requirements.txt              依赖（mcp==1.30.0）
├─ launch_blender.ps1            Windows 启动助手（默认 GUI）
├─ blender_mcp\                  MCP 服务端包
│  ├─ app.py config.py bridge.py models.py
│  └─ tools\  meta.py scene.py geometry_nodes.py shape_keys.py drivers.py launcher.py
├─ blender\
│  ├─ blender_mcp_addon.py       Blender 侧全部逻辑（非阻塞 socket + 命令实现 + 面板）
│  └─ bootstrap.py               在 Blender 内启动桥（控制台 / --python / 无头 pump）
├─ tests\
│  ├─ test_unit_validation.py    纯 Python 单元/协议测试（56 项）
│  ├─ test_review_transport.py   连接期限、配置与启动参数回归（7 项）
│  ├─ blender_integration.py     真实 Blender 集成测试（48 项，产出 demo .blend）
│  ├─ blender_review_regressions.py  引用、恢复与轮询回归（41 项）
│  ├─ test_mcp_stdio.py          真实 MCP stdio 客户端端到端测试（78 项）
│  ├─ stdio_pipe_transport.py    stdio 客户端传输（见 §9 已知环境限制）
│  ├─ probes\                    Blender 5.2 API 探针（实现依据，可复现）
│  └─ reports\                   测试报告（JSON + TEST_REPORT.md）
├─ .venv\                        自包含虚拟环境（含 mcp）
└─ .runtime\                     token / 日志 / 测试临时文件（不入库）
<workspace>\Project\demo\parametric_demo.blend    集成测试产出的可编辑演示文件
```

---

## 2. 环境要求

| 项 | 值 |
| --- | --- |
| Python（MCP 服务端） | 3.10+，实测 3.12.7（`D:\SoftWare\anaconda\python.exe`） |
| 依赖 | `mcp>=1.26,<2`（唯一第三方依赖） |
| Blender | 实测 **5.2.1 LTS**，路径 `D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe`（脚本会自动发现 `Version\*\blender.exe`） |
| 平台 | Windows（桥本身是跨平台的；启动助手是 PowerShell） |

---

## 3. 安装（一次）

```powershell
# 1) 自包含虚拟环境（已随本仓库创建；如需重建）
D:\SoftWare\anaconda\python.exe -m venv D:\AI\MCP\Blender\MCP\.venv

# 2) 安装依赖（有网）
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe -m pip install -r D:\AI\MCP\Blender\MCP\requirements.txt

# 3) 校验
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe -c "import mcp, sys; print(mcp.__file__, sys.version)"
```

**不需要**给 Blender 安装任何东西；`MCP\blender\*.py` 只用 `bpy` + 标准库。

---

## 4. 启动 Blender（四种方式，任选其一）

> 共同点：都会在 `127.0.0.1:9876` 上开桥（如果该端口已有一个**健康**的桥，会直接复用而不新开）。
> 令牌文件默认 `MCP/.runtime/token`，首次运行自动生成（64 位十六进制，权限尽量收紧）。

**(a) 已经打开的 Blender（推荐日常使用）** — 文本编辑器 → 打开 `MCP\blender\bootstrap.py` → 运行脚本；
或在 Python 控制台：

```python
import sys; sys.path.insert(0, r"D:\AI\MCP\Blender\MCP\blender")
import bootstrap; bootstrap.main([])
```

之后 GUI 由 `bpy.app.timers` 在主线程轮询 socket（非阻塞），并在 属性 → 场景 → **Blender MCP Bridge** 面板里显示状态 / 启停按钮。

**(b) 命令行启动 GUI Blender**

```powershell
D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe --python D:\AI\MCP\Blender\MCP\blender\bootstrap.py
```

**(c) 启动助手（PowerShell，默认 GUI）**

```powershell
powershell -ExecutionPolicy Bypass -File D:\AI\MCP\Blender\MCP\launch_blender.ps1
# 无头 + 隔离配置（测试用）：
powershell -ExecutionPolicy Bypass -File D:\AI\MCP\Blender\MCP\launch_blender.ps1 `
    -Background -FactoryStartup -IsolateConfig -PumpSeconds 600
```

- 默认 **GUI、不加 `--factory-startup`**，保留用户偏好；`-FactoryStartup` 仅用于受控测试。
- 助手**从不杀进程**（包括它自己启动的）；要停就在 Blender 里 `bpy.ops.blendermcp.stop_bridge()` 或关掉 Blender。

**(d) 无头 Blender（自动化/CI）**

```powershell
D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe --background --factory-startup `
    --python D:\AI\MCP\Blender\MCP\blender\bootstrap.py -- --mcp-pump --mcp-pump-seconds 600
```

> **为什么无头必须 `--mcp-pump`**：`--background` 没有事件循环，`bpy.app.timers` 永不触发。
> `bootstrap.py --mcp-pump` 用普通循环手动调用**同一个**非阻塞 `poll()`，仍在 Blender 主线程上执行所有 `bpy` 调用。

bootstrap 参数（放在 `--` 之后）：`--mcp-host`、`--mcp-port`、`--mcp-token-file`、`--mcp-project-dir`、
`--mcp-pump`、`--mcp-pump-seconds`、`--mcp-force`、`--mcp-no-timer`、`--mcp-quiet`。

---

## 5. 让 Codex 连上

本机已注册名为 **`blender`** 的 Codex MCP 服务。重新加载 MCP 或重启 Codex 后可用。
下面的配置供重新安装或迁移使用；项目内模板见 `codex-config.toml`。
Codex 的服务登记保存在其应用配置中，MCP 的源码、Git、文档与日志均在本目录。

**方式一：配置文件（`~/.codex/config.toml`）**

```toml
[mcp_servers.blender]
command = "D:\\AI\\MCP\\Blender\\MCP\\.venv\\Scripts\\python.exe"
args = ["D:\\AI\\MCP\\Blender\\MCP\\server.py"]
env = { BLENDER_MCP_BLENDER = "D:\\AI\\MCP\\Blender\\Version\\5_2_1_LTS\\blender.exe", BLENDER_MCP_PORT = "9876", BLENDER_MCP_TIMEOUT = "45" }
```

**方式二：CLI 注册**（语法取自本机 `codex mcp add --help`）

```powershell
codex mcp add blender --env "BLENDER_MCP_BLENDER=D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe" `
  --env BLENDER_MCP_PORT=9876 --env BLENDER_MCP_TIMEOUT=45 -- `
  "D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe" "D:\AI\MCP\Blender\MCP\server.py"
```

手动跑一次服务端（排查用；**stdout 只走 MCP 协议**，日志走 stderr）：

```powershell
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe D:\AI\MCP\Blender\MCP\server.py
```

连接顺序建议：先 `blender_health`；失败就 `blender_launch_blender`（或手工按 §4 启动 Blender）后重试。

一个桥接端口连接一个 Blender 实例。健康桥会优先复用；切换版本前关闭原桥，或配置另一端口。

---

## 6. 工具清单（38 个）

所有错误都是 **MCP 错误（`isError=true`）**，附带机器可读的 `code`，不会用"成功值里带 error"的方式蒙混。

### 6.1 健康/环境（6）
| 工具 | 说明 |
| --- | --- |
| `blender_health` | 桥状态、协议版本、Blender/ Python 版本、PID、当前 .blend、场景、对象/节点组数量、命令清单 |
| `blender_server_status` | 桥计数器：客户端数、成功/失败/未授权次数、最后一个错误 |
| `blender_list_commands` | Blender 侧底层命令清单（核对插件版本） |
| `blender_ping` | 最省的一次往返 |
| `blender_bridge_probe` | 不抛异常的连通性探测（reachable + health/hint） |
| `blender_environment` | 服务端 venv、workspace、Project 根、token 是否存在、桥地址、超时、bootstrap 路径 |

### 6.2 场景/对象（9）
`blender_scene_summary`、`blender_list_objects`、`blender_object_info`（含**求值后**网格顶点/边/面数、
世界包围盒、mesh signature、修改器与 GN 参数、形态键、驱动器、自定义属性）、`blender_create_mesh_object`、
`blender_create_primitive`、`blender_set_custom_property`（object/scene，set/get/remove，被驱动时会警告）、
`blender_save_blend`、`blender_set_frame`、`blender_update_depsgraph`。

### 6.3 几何节点（13，主线）
`blender_create_node_group`（类型化接口：名称/类型/默认值/范围/子类型）、`blender_create_node`、
`blender_set_node_property`、`blender_set_socket_default`、`blender_link_sockets`、`blender_unlink_socket`、
`blender_remove_node`、`blender_inspect_node_group`（节点名/索引/类型/**socket identifier**/默认值/连接/接口/使用者/驱动器）、
`blender_attach_geometry_nodes_modifier`、`blender_set_modifier_input`、`blender_list_node_types`、
`blender_describe_node_type`（实例化一个临时节点，报告**真实 socket** 与 RNA 属性/枚举）、`blender_batch_graph_edit`。

### 6.4 形态键（5）
`blender_inspect_shape_keys`、`blender_create_shape_key`、`blender_set_shape_key_coordinates`（坐标或增量，逐点/子集）、
`blender_set_shape_key`（影响值/静音/滑块范围，返回求值结果与 signature）、`blender_remove_shape_key`。

### 6.5 驱动器（4）
`blender_inspect_drivers`、`blender_add_driver`、`blender_update_driver`、`blender_remove_driver`。
owner 支持 `object` / `shape_keys` / `node_group` / `scene`；变量仅支持 `SINGLE_PROP` 与 `TRANSFORMS`。

### 6.6 启动（1）
`blender_launch_blender`（默认 GUI；`background=true` 无头；已有健康桥则直接复用，不启动新进程；子进程输出写日志文件，绝不混入 MCP stdout）。

---

## 7. 关键语义（务必先读）

1. **先看再连**：Blender 的 socket 显示名会被本地化、跨版本会变，**identifier 才是稳定句柄**。
   用 `blender_inspect_node_group` / `blender_describe_node_type` 拿 identifier 再连。
   例：`GeometryNodeTransform` 在 5.2 里的默认节点名是 **"Transform Geometry"**，不要假设。
2. **保持过程化**：新增 GN 修改器是叠加的，不会烘焙/删除已有修改器与参数。
3. **批量编辑不是事务**：`blender_batch_graph_edit` 顺序执行并逐条报告；失败时 `completed` 里的是**已经生效**的，
   工具**不声称回滚**（响应里 `rollback: false` 明说）。
4. **形态键要求原始网格拓扑**：一个 key 每个顶点一份坐标，顶点数必须等于网格顶点数。
   - 没有 key 时本工具会**先建立 `Basis`** 再建目标 key（Blender 会把第一个 key 直接当作基准，所以必须显式建 Basis）；
   - `Basis` 默认**写保护**（`allow_basis=true` 才放行）；
   - **是否受形态键影响取决于几何流**：几何节点若继续处理原始网格，可以保留形态键的变形；若从基本体生成独立网格，
     基础网格上的 key 可能不影响该输出。可设计保留原始几何的节点流，或分别使用形态键对象和过程化生成对象。
     示例中的 `GN_Morph` 用于展示独立生成几何的情况，不代表所有修改器都忽略形态键。
5. **驱动器在帧变化 / 依赖图更新时求值**：改完驱动器输入后调 `blender_set_frame` 或 `blender_update_depsgraph` 再读值；
   返回值里的 `evaluated_value` / `evaluated` 已经是强制更新后的真实结果。
   - **被驱动的属性不能被手写覆盖**：`blender_set_shape_key` / `blender_set_custom_property` 会返回 `driven: true` 与 `warnings`，
     这时应该改驱动器的输入，而不是硬写目标属性。
6. **`data_path` 是属性路径而不是代码**：一律用 `ID.path_resolve` 校验，不接受 `eval`/`exec`。
   驱动器表达式会被 `ast.parse` 解析并按白名单校验（只允许字面量、变量名、算术/比较/布尔运算、三元表达式、
   以及 `sin/cos/tan/sqrt/pow/floor/...` 这类数学函数；属性访问、下标、lambda、推导式、赋值、import 全部拒绝）。
7. **保存路径受限**：只能相对 `<workspace>\Project`；绝对路径、`..`、符号链接/junction 组件一律拒绝；
   默认 `overwrite=false`，覆盖必须显式 `overwrite=true`。
8. **不求值不结论**：GN 参数、形态键影响、驱动器都以上下文里返回的求值网格统计/包围盒为准，不要只看属性回读。

---

## 8. 端到端示例（几何节点 → 参数 → 形态键 → 驱动器 → 保存）

```text
1) blender_health                                  → 确认 Blender 5.2.1 在跑
2) blender_describe_node_type("GeometryNodeMeshGrid")
                                                   → 拿到真实 socket：Size X / Vertices X / Vertices Y ...
3) blender_create_node_group(
     name="ParametricPanel", if_exists="replace",
     interface=[{"name":"Size","socket_type":"NodeSocketFloat","in_out":"INPUT",
                 "default_value":2.0,"min_value":0.1,"max_value":50.0,"subtype":"DISTANCE"},
                {"name":"Density","socket_type":"NodeSocketInt","in_out":"INPUT","default_value":4},
                {"name":"Height","socket_type":"NodeSocketFloat","in_out":"INPUT","default_value":1.0},
                {"name":"Geometry","socket_type":"NodeSocketGeometry","in_out":"OUTPUT"}])
4) blender_batch_graph_edit(group="ParametricPanel", operations=[
     {"op":"create_node","node_type":"GeometryNodeMeshGrid","name":"Grid"},
     {"op":"create_node","node_type":"ShaderNodeCombineXYZ","name":"HeightVec"},
     {"op":"create_node","node_type":"GeometryNodeTransform","name":"XForm"},
     {"op":"link_sockets","from_node":"Grid","from_socket":"Mesh","to_node":"XForm","to_socket":"Geometry"},
     {"op":"link_sockets","from_node":"XForm","from_socket":"Geometry",
      "to_node":"Group Output","to_socket":"Geometry"},
     {"op":"link_sockets","from_node":"Group Input","from_socket":"Size","to_node":"Grid","to_socket":"Size X"},
     {"op":"link_sockets","from_node":"Group Input","from_socket":"Density","to_node":"Grid","to_socket":"Vertices X"},
     {"op":"link_sockets","from_node":"Group Input","from_socket":"Height","to_node":"HeightVec","to_socket":"Z"},
     {"op":"link_sockets","from_node":"HeightVec","from_socket":"Vector",
      "to_node":"XForm","to_socket":"Translation"},
     {"op":"set_socket_default","node":"Grid","socket":"Vertices Y","value":4}])

5) blender_create_mesh_object(name="GN_Panel", vertices=[[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0]], faces=[[0,1,2,3]])
6) blender_attach_geometry_nodes_modifier(object="GN_Panel", group="ParametricPanel",
                                          parameters={"Size":2.0,"Density":4,"Height":1.0})
       → evaluated.vertices == 16, bounds z == 1.0
7) blender_set_modifier_input(object="GN_Panel", input="Size", value=6.0)
       → evaluated.bounds.size[0] == 6.0     ← 参数真的改变了求值几何
8) blender_create_primitive(kind="uv_sphere", name="Morph_Sphere", radius=1.0)
9) blender_create_shape_key(object="Morph_Sphere", name="ScaleX", value=0.0, slider_max=1.0)
       → 自动建 Basis，keys == ["Basis","ScaleX"]
10) blender_set_shape_key_coordinates(object="Morph_Sphere", key_name="ScaleX",
                                      deltas=[[0.5*x,0,0] for each vertex])   # 共 482 项
11) blender_set_shape_key(object="Morph_Sphere", key_name="ScaleX", value=1.0)
       → evaluated.bounds.max[0] ≈ 1.5（半径 1 → 1.5）
12) blender_set_custom_property(owner_kind="object", owner_name="Morph_Sphere", key="bulge", value=0.0)
13) blender_add_driver(owner_kind="shape_keys", owner_name="Morph_Sphere",
                       data_path='key_blocks["ScaleX"].value', expression="bulge",
                       variables=[{"name":"bulge","type":"SINGLE_PROP","targets":[
                                    {"id_type":"OBJECT","id_name":"Morph_Sphere","data_path":"[\"bulge\"]"}]}])
14) blender_set_custom_property(owner_kind="object", owner_name="Morph_Sphere", key="bulge", value=1.0)
    blender_set_frame(frame=4)
       → 形态键影响被驱动到 1.0，求值包围盒随之变化
15) blender_add_driver(owner_kind="object", owner_name="Driver_Cube", data_path="rotation_euler", index=2,
                       expression="phase * 2",
                       variables=[{"name":"phase","type":"SINGLE_PROP","targets":[
                                    {"id_type":"OBJECT","id_name":"Driver_Cube","data_path":"[\"phase\"]"}]}])
16) blender_save_blend(relative_path="demo/my_scene.blend")     # 第二次不加 overwrite=true 会报 file_exists
```

真实跑通这套流程的产物：`Project\demo\parametric_demo.blend`（可编辑节点组 + 形态键 + 4 处驱动器，
重新打开后仍可改参数：`blender_set_modifier_input` 改 Density 会继续改变求值顶点数）。

---

## 9. 架构与协议

```
Codex ──stdio(JSON-RPC/MCP)──> MCP/server.py ──TCP 127.0.0.1:9876(逐行 JSON+token)──> Blender 插件(主线程 poll)
```

- **分帧**：请求/响应都是「一行一个 JSON 对象 + `\n`」，UTF-8，双向都有 8 MiB 上限；超限/坏 JSON/坏 token 都返回
  明确错误码（`frame_too_large` / `bad_request` / `unauthorized`）并关闭连接。
- **鉴权**：每条请求都带 token；用 `secrets.compare_digest` 比较；**任何日志/错误都不包含 token**。
- **单线程、无 worker**：socket 循环是非阻塞的，由 `bpy.app.timers`（GUI）或手动 pump（无头）驱动，
  命令在 Blender **主线程**执行 —— 没有后台线程碰 `bpy`。
- **不回放**：超时/断连只报错，绝不自动重发（尤其不改动属性的命令）。
- **不做任意代码执行**：桥命令是固定清单；驱动器表达式是唯一表达式通道，且先解析再校验。
- 命令与工具一一对应，`blender_list_commands` 可列出底层命令名。

---

## 10. 已知限制（第一版范围内的诚实说明）

1. `blender_batch_graph_edit` **没有事务/回滚**：部分失败时按 `completed` 继续修。
2. `blender_update_driver` 若新表达式语义无效，会**自动恢复原表达式与变量**并报 `invalid_expression`
   （实测恢复成功、`is_valid=True`）；`blender_add_driver` 新建后若 Blender 判定无效则**撤销本次新建**并报错。
3. 形态键只作用于对象**原始网格**；生成几何的修改器会盖掉它（见 §7.4）。
4. GN 修改器参数写入后必须给对象打 tag 才会重算（`MCP\blender\blender_mcp_addon.py` 里已内建，
   这是 Blender 5.2 实测行为，见 `tests\probes\probe_blender_api7.py`）。
5. 驱动器变量只支持 `SINGLE_PROP` / `TRANSFORMS`（不支持 `ROTATION_DIFF` / `LOC_DIFF`）。
6. 保存只允许写 `<workspace>\Project` 下；不提供删除/重命名/移动文件之类的破坏性工具。
   保存时会临时关闭"预览图"和"版本备份"，因此**不会**在项目目录里额外产生 `.blend1`。
7. 同一时刻 Blender 只服务一个 socket 循环；一个耗时命令执行期间新请求要排队（单线程，无并发执行）。
8. 形态键坐标载荷**先整体校验再写入**（中间项非法时不会留下部分写入）；驱动器表达式在写入前被解析校验。

---

## 11. 故障排查

| 现象 | 处理 |
| --- | --- |
| 工具报 `bridge_unavailable` | Blender 没开桥：按 §4 启动，或调用 `blender_launch_blender` |
| 报 `unauthorized` | 两侧 token 不一致：确认都用 `MCP\.runtime\token`（`--mcp-token-file` 要一致） |
| 报 `frame_too_large` | 请求过大：`create_mesh_object` 的顶点/面分批传 |
| 改了 GN 参数但几何没变 | 用带 `evaluated`/`signature` 的返回判断；若属性被**驱动器**接管会返回 `driven: true` |
| 形态键值写不进去 | 该 key 被驱动器驱动（`driven: true`）→ 改驱动器输入（如自定义属性）而不是硬写 |
| 保存时多出 `.blend1` | 本版本会在保存期间关闭版本备份；若你手动在 Blender 里保存则仍可能出现 |
| 端口被占用 | `bind_failed`：换 `--mcp-port`，或复用已在跑的那个桥 |
| 保存时报 `image.write ERROR ... thumbnails` | 无害：Blender 想往系统缩略图缓存写预览图，被受限环境拒绝；**.blend 已保存成功**，`use_save_preview_images` 已在保存期间关闭 |
| 无头模式下桥不响应 | 忘了 `-- --mcp-pump`（`--background` 不会跑 timers） |

---

## 12. 测试

最终正常账户环境共 **230 项检查通过**（56 + 7 + 48 + 41 + 78），包含官方 SDK 自带 stdio 传输。
几何节点组复用、对象/材质等指针参数的 JSON 引用格式见 `docs/GN_REFERENCES.md`。

```powershell
# 1) 单元/协议（不需要 Blender）
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe D:\AI\MCP\Blender\MCP\tests\test_unit_validation.py

# 2) 真实 Blender 集成（GN 参数 / 形态键 / 驱动器 求值断言 + 产出 demo .blend）
D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe --background --factory-startup --python-exit-code 1 `
    --python D:\AI\MCP\Blender\MCP\tests\blender_integration.py

# 3) 真实 MCP stdio 客户端端到端（含负例、鉴权、端口与进程收尾）
D:\AI\MCP\Blender\MCP\.venv\Scripts\python.exe D:\AI\MCP\Blender\MCP\tests\test_mcp_stdio.py
```

三条命令的实测结果、环境限制与 Blender 5.2 API 差异见 **`MCP\tests\reports\TEST_REPORT.md`**。

运行全部套件：`tests/run_all_tests.ps1`。在本机正常账户环境可设置
`BLENDER_MCP_FORCE_SDK_STDIO=1` 使用官方 SDK 管道；受限构建环境的兼容传输仅用于测试。
