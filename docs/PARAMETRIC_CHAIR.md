# 参数化木椅：MCP 客户端配方（examples/parametric_chair.py）

已经在本机 Blender **5.2.1 LTS** 中通过官方 MCP SDK 执行全部阶段，得到可编辑模型。

- 工程：`D:/AI/MCP/Blender/Project/chairs/parametric_chair.blend`（794,766 字节）。
- 预览：`D:/AI/MCP/Blender/Project/previews/parametric_chair.png`（960×720，MCP 原生图片返回）。
- 对象 `Parametric_Chair`；修改器 `Parametric Chair`；节点组 `GN_Parametric_Chair`，65 个节点、144 条链接、8 个参数，修改器未应用。
- 默认座宽 48 cm、座深 46 cm、座高 45 cm、座厚 3.5 cm、腿截面 4.5 cm，靠背长 42 cm、后倾 0.10 rad（约 5.73°），第一横档轴向位置 12 cm。
- 座宽 0.48→0.60、座高 0.45→0.52 的真实求值变化及复位均通过；9 项几何断言全部通过。默认包围盒尺寸 `[0.48, 0.501818, 0.870148]` m。
- GUI 通过 `blender_present_object` 选择并隔离椅子、框选完整模型，保留修改器参数面板。用户随后要求不切换前台，后续所有显示调用均 `foreground=false`；后台建模与渲染不使用 computer-use。

操作日志在 `MCP/.runtime/logs`，阶段证据在 `MCP/.runtime/chair_*.log`。MCP 配方代码与本文入库；模型、预览和运行日志不入库。

下文介绍配方用法、节点算式、工具调用和复现方式。

---

## 1. 目的与安全契约

配方通过**官方 MCP SDK** 驱动本仓库的 MCP 服务端，用**具名 typed 工具**把一把木椅建成可编辑的几何节点模型：

- 传输：`mcp.ClientSession` + `mcp.client.stdio.stdio_client` 启动 `<MCP>\server.py`（默认用 `<MCP>\.venv\Scripts\python.exe`）。
- 端口：子进程环境变量 `BLENDER_MCP_PORT`；`--port` 默认取环境变量 `BLENDER_MCP_PORT`，再默认 `9876`。
- 每次调用**先 `blender_health`**；任何工具返回 `isError` 立即抛错并以退出码 2 结束（不做"看起来成功"的猜测）。
- **只有 typed 工具**：没有 `bpy`、没有直连 bridge 的 JSON、没有任意代码执行、不启动/不结束 Blender。
- **不删除、不替换既有资产**：同名节点组/对象已存在时阶段直接报错退出；不使用 `if_exists="replace"`。
- 采集器对象是**原点处 1 顶点空网格**（真实米制）；可见几何 100% 由节点组生成。

退出码：`0` 成功，`2` 有界失败（错误信息在 stderr），`130` 中断。

---

## 2. 快速开始

前置条件：Blender 已运行且桥已监听（`blender\bootstrap.py` 或 `launch_blender.ps1`），端口与 `--port` 一致。

```powershell
# 在 <MCP> 目录下，按顺序每个阶段一次调用
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase inspect
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase seat
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase legs
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase back
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase finish
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase verify
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase save

# 第二把椅子（不改动第一把）：统一加后缀
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase seat --suffix _v2
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--phase` | 必填 | `inspect`\|`seat`\|`legs`\|`back`\|`finish`\|`verify`\|`save` |
| `--port` | `BLENDER_MCP_PORT` 或 `9876` | 传给服务端子进程的 `BLENDER_MCP_PORT` |
| `--suffix` | 空 | 只影响数据块名：`Parametric_Chair<suffix>` / `GN_Parametric_Chair<suffix>` |
| `--python` | `<MCP>\.venv\Scripts\python.exe` | 运行 `server.py` 的解释器 |
| `--timeout` | `60` 秒 | 单次 MCP 调用读超时（预览调用固定用更大的预算） |
| `--overwrite-save` | 关闭 | 显式允许覆盖已存在的 `.blend`（默认 false，符合配方要求） |

命名与输出：对象 `Parametric_Chair`、节点组 `GN_Parametric_Chair`、修改器 `Parametric Chair`、
集合 `Parametric Chairs`、保存到 `Project/chairs/parametric_chair.blend`、预览 `Project/previews/parametric_chair.png`。

---

## 3. 阶段表

| 阶段 | 工具调用（按顺序） | 图操作 | 产物 / 断言 |
| --- | --- | --- | --- |
| `inspect` | `blender_health` → `blender_scene_summary` → `blender_list_objects` → `blender_describe_node_type` ×5 →（若已存在）`blender_inspect_node_group` | 无（只读） | 打印单位/集合/节点组、5 个节点类型的**真实插槽标识符**、椅子现有链接数、建议的下一个阶段 |
| `seat` | `blender_health` → `blender_scene_summary` → `blender_list_objects` → `blender_create_node_group` → `blender_inspect_node_group` → `blender_batch_graph_edit` → `blender_create_mesh_object` → `blender_attach_geometry_nodes_modifier` → `blender_object_info` → `blender_inspect_node_group` | 1 个事务批次，**21** 个操作（7 个节点、2 个默认值、12 条链接） | 节点组 + 采集器对象 + 修改器；座椅已连到 Group Output（立刻可见）；断言 join 多输入链接 = 1 |
| `legs` | `blender_health` → `blender_inspect_node_group` → `blender_list_objects`/`blender_object_info` → `blender_batch_graph_edit` → `blender_inspect_node_group` | 1 个事务批次，**64** 个操作（17 个节点、5 个默认值、42 条链接） | 4 条腿追加进 join；断言 join 链接 = 5 |
| `back` | 同上 + `blender_batch_graph_edit` | **2** 个事务批次，共 **133** 个操作（39 个节点、4 个默认值、90 条链接；首批发 100） | 2 根后腿柱 + 3 根横档追加进 join；断言 join 链接 = 10 |
| `finish` | `blender_health` → `blender_list_objects`/`blender_object_info` → `blender_inspect_node_group` → `blender_set_custom_property` ×3 → `blender_object_info` | 无图操作 | 对象自定义属性 `recipe` / `version` / `description`；打印全部修改器参数 |
| `verify` | `blender_health` → `blender_inspect_node_group` → `blender_object_info` → `blender_set_modifier_input` ×4 → `blender_object_info` → `blender_capture_preview` | 只改参数并复位 | 9 项检查（见 §7）+ 960×720 预览 |
| `save` | `blender_health` → `blender_list_objects`/`blender_object_info` → `blender_save_blend` | 无 | `Project/chairs/parametric_chair.blend`（`overwrite=false` 除非显式给 `--overwrite-save`） |

批次规划来自离线干跑实测（§9）：`batch sizes: [21, 64, 100, 33]`，最大 100，每批 `transactional=true`，
且**先建节点、再设默认值、最后连链接**；同一批次内不会出现"链接引用尚未创建的节点"。

阶段依赖：`legs` 需要 `seat`（`Unit_Cube`/`Join_Parts`），`back` 需要 `legs`（复用 `Leg_X_Half`/`Leg_X_Neg`/`Leg_Y_Half`）。
缺节点时阶段直接报错并提示按序执行，不做隐式补齐或删除。

---

## 4. 暴露的参数（组输入，英文名，真实米制）

| 名称 | 默认 | 范围 | 单位 | 含义 |
| --- | --- | --- | --- | --- |
| `Seat Width` | 0.48 | 0.30–0.80 | m | 座面 X 方向尺寸（左右） |
| `Seat Depth` | 0.46 | 0.30–0.80 | m | 座面 Y 方向尺寸（前后，**椅背为 +Y**） |
| `Seat Height` | 0.45 | 0.25–0.70 | m | 座面上表面距地高度 |
| `Seat Thickness` | 0.035 | 0.010–0.100 | m | 座面板厚 |
| `Leg Size` | 0.045 | 0.020–0.100 | m | 腿、后柱、横档的方形截面边长 |
| `Back Height` | 0.42 | 0.15–0.70 | m | 后柱沿柱轴长度（座面以上） |
| `Back Tilt` | 0.10 | 0.00–0.35 | **rad** | 后倾角；0 = 竖直，正值向 +Y 倾 |
| `Rail Height` | 0.12 | 0.05–0.30 | m | 第一根横档距座面的高度（沿柱轴） |

长度类用 `subtype="DISTANCE"`（仅影响 UI 显示单位）；`Back Tilt` **不设** subtype，避免 UI 用角度
显示而与"弧度"语义混淆。类型均为 `NodeSocketFloat`；输出接口为 `NodeSocketGeometry`（名为 `Geometry`）。

---

## 5. 几何节点架构

### 5.1 对象与集合

- 对象 `Parametric_Chair`，位置 `(0, 0, 0)`，真实米制。
- 基础网格：**1 个原点顶点、无边无面**。`blender_create_mesh_object` 会拒绝空顶点列表
  （`MCP\blender\blender_mcp_addon.py` 的 `_cmd_create_mesh_object`，约 2148 行），所以这是工具允许的
  最小载具网格；它不参与求值（Group Output 只连了 join 节点），因此可见几何全部来自节点组。
- 集合 `Parametric Chairs`：若已存在则使用；**不存在时没有具名工具可以创建集合**，此时对象落到场景集合
  并打印 `[warn]`（§10.2）。

### 5.2 节点链（单一节点组 `GN_Parametric_Chair`）

```text
Group Input ──┬─ 参数 → Math/CombineXYZ（所有派生量）
              └─ （不参与几何，几何全部程序化生成）

Unit_Cube(GeometryNodeMeshCube, Size=[1,1,1])
   │  Mesh
   ├────────────►  Seat_Transform  ─┐        （共享同一个单位立方体，
   ├────────────►  Leg_*_Transform ─┤         每个部件只做 Scale+Translation，
   ├────────────►  Back_Post_*     ─┤         椅背部件再加 Rotation）
   └────────────►  Back_Rail_*     ─┘
                                    │  Geometry（多输入，逐条追加）
                                    ▼
                        Join_Parts (GeometryNodeJoinGeometry)
                                    │
                                    ▼
                          Group Output.Geometry
```

节点总计（离线干跑）：65 个 —— `NodeGroupInput` 1、`NodeGroupOutput` 1、`MeshCube` 1、`Transform` 10
（座面 1 + 腿 4 + 后柱 2 + 横档 3）、`CombineXYZ` 15、`Math` 36、`JoinGeometry` 1；链接 144 条。

### 5.3 每个部件的算式（全部由 Math/CombineXYZ 表达，不烘焙网格）

| 部件 | Scale（CombineXYZ） | Translation | Rotation |
| --- | --- | --- | --- |
| 座面 | `(SeatWidth, SeatDepth, SeatThickness)` | `(0, 0, SeatHeight − SeatThickness/2)` | — |
| 腿 ×4 | `(LegSize, LegSize, SeatHeight − SeatThickness)` | `(±(SeatWidth−LegSize)/2, ±(SeatDepth−LegSize)/2, (SeatHeight−SeatThickness)/2)` | — |
| 后柱 ×2 | `(LegSize, LegSize, BackHeight)` | `(±(SeatWidth−LegSize)/2, legY + (BackHeight/2)·sin θ, SeatHeight + (BackHeight/2)·cos θ)` | `(−θ, 0, 0)` |
| 横档 ×3 | `(SeatWidth − LegSize, LegSize, LegSize)` | `(0, legY + dᵢ·sin θ, SeatHeight + dᵢ·cos θ)` | `(−θ, 0, 0)` |

其中 `θ = Back Tilt`（弧度），`legY = (SeatDepth − LegSize)/2`（后腿中心 y，椅背在 +Y），
横档沿柱轴的距离 `dᵢ = RailHeight + i·(BackHeight − RailHeight)/3`（i = 0,1,2；默认 0.12 / 0.22 / 0.32），
横档长度取两柱中心距 `SeatWidth − LegSize`，两端各嵌入柱内半个柱宽。

**倾斜方向**：绕 X 轴旋转 `−θ`。对柱上局部点 `(y,z)`：`y' = y·cosθ + z·sinθ`，`z' = −y·sinθ + z·cosθ`，
即柱顶向 **+Y** 倾（`z>0` 的点得到正的 `y'`）。θ = 0 时后柱竖直，算式退化为同一形式，未使用的参数不会
被"假装使用"。

### 5.4 追加语义（append，而不是替换）

`Join_Parts` 的 `Geometry` 是**多输入**插槽，`legs`/`back` 阶段**只追加链接**（`replace` 用工具默认 true，
即"允许在已链接的输入上再加一条"，Blender 对多输入插槽是累加）。每个追加阶段结束后：
`blender_inspect_node_group` 重新读图 → 统计指向 `Join_Parts.Geometry` 的链接 →
断言 **1 / 5 / 10**（seat / legs / back，含来源节点名）。如果 Blender 的行为是"替换"，阶段会**硬失败并打印
证据**，而不是让缺件的椅子静默通过（§10.3）。

---

## 6. 工具签名与"运行期发现"策略

配方使用的工具签名全部取自本地源码（下述行号基于当前工作区版本）：

| 工具 | 关键参数 | 本地源码证据 |
| --- | --- | --- |
| `blender_health` | 无 | `blender_mcp\tools\meta.py:16` |
| `blender_scene_summary` | `include_objects` | `blender_mcp\tools\scene.py:16` |
| `blender_list_objects` | `name_contains`, `type`, `limit` | `blender_mcp\tools\scene.py:27` |
| `blender_object_info` | `name`, `include_evaluated` | `blender_mcp\tools\scene.py:45` |
| `blender_create_mesh_object` | `name`, `vertices`, `faces`, `collection`, `location` | `blender_mcp\tools\scene.py:58` |
| `blender_create_node_group` | `name`, `interface[]`, `with_input_output`, `geometry_output`, `if_exists` | `blender_mcp\tools\geometry_nodes.py:57` |
| `blender_describe_node_type` | `node_type` | `blender_mcp\tools\geometry_nodes.py:320` |
| `blender_inspect_node_group` | `name`, `include_sockets` | `blender_mcp\tools\geometry_nodes.py:230` |
| `blender_batch_graph_edit` | `group`, `operations[]`, `transactional`, `continue_on_error` | `blender_mcp\tools\geometry_nodes.py:332` |
| `blender_link_sockets`（单步，本配方只用批次内等价操作） | `group`,`from_node`,`from_socket`,`to_node`,`to_socket`,`replace` | `blender_mcp\tools\geometry_nodes.py:171`、`blender\blender_mcp_addon.py:2853` |
| `blender_attach_geometry_nodes_modifier` | `object`, `group`, `modifier_name`, `parameters`, `reuse_existing` | `blender_mcp\tools\geometry_nodes.py:243` |
| `blender_set_modifier_input` | `object`, `input`, `value`, `modifier`, `include_evaluated` | `blender_mcp\tools\geometry_nodes.py:271` |
| `blender_set_custom_property` | `owner_kind`, `key`, `value`, `owner_name`, `remove` | `blender_mcp\tools\scene.py:122` |
| `blender_save_blend` | `relative_path`, `overwrite`, `compress` | `blender_mcp\tools\scene.py:149` |
| `blender_capture_preview` | `object`, `path`, `mode`, `view`, `width`, `height`, `samples`, `timeout`, `overwrite`, `transparent` | `blender_mcp\tools\preview.py:113` |

**插槽一律运行期发现，不靠猜名字**：

- 节点类型 id（`GeometryNodeMeshCube` / `GeometryNodeTransform` / `GeometryNodeJoinGeometry` /
  `ShaderNodeCombineXYZ` / `ShaderNodeMath`）取自既有测试与探针已实测的证据
  （`tests\blender_integration.py`、`tests\probes\probe_blender_api*.py`），并在 `inspect` 阶段用
  `blender_describe_node_type` 逐个确认存在。
- 每个插槽的 `identifier` 来自 `blender_describe_node_type`（单节点）或 `blender_inspect_node_group`
  （组接口与 Group Input/Output 节点）的**实际返回**；找不到就报错并列出可用插槽，不做同名假设。
- `ShaderNodeMath` 的三个输入**显示名都是 `Value`**，因此操作数用"索引 → 运行期发现的 identifier"
  映射（`Value` / `Value_001`），不按显示名寻址：规格上允许的索引用法，其依据是本地探针/测试已确立的真实
  identifier 结构；若某版本名字不同，配方会用发现到的 identifier，而不是硬编码字符串。
- 组创建后立即校验：8 个输入名齐全且无多余项、`Geometry` 输出存在、Group Input 节点确有对应 identifier
  的输出插槽、Group Output 确有几何输入插槽；任一不符即报错（节点组保留、不删不替换）。
- `set_socket_default` / `link_sockets` 的批次操作**只传命令本身接受的参数**（addon 把这些参数直接
  `**kwargs` 转发给命令函数），因此不附加任何额外字段，避免整批事务回滚。

---

## 7. `verify` 阶段验证什么

读取当前修改器参数作为基线（不是硬编码 0.48/0.45），逐个改参数、读求值包围盒、再复位：

| 检查 | 期望 | 容差 |
| --- | --- | --- |
| `Seat Width` 0.48 → 0.60 | 包围盒宽度 +0.12 | 0.010 |
| `Seat Width` 复位 | 回到基线宽度 | 0.005 |
| `Seat Height` 0.45 → 0.52 | 最高点 max_z +0.07 | 0.010 |
| `Seat Height` 复位 | 回到基线 max_z | 0.005 |
| width == `Seat Width` | 0.48 | 0.010 |
| min_z == 0（腿落地） | 0 | 0.010 |
| max_z == `SeatHeight + BackHeight·cosθ + (LegSize/2)·sinθ` | 0.870148（默认参数） | 0.010 |
| max_y == 后柱最高后角（向 +Y 倾） | 0.271818（默认参数） | 0.010 |
| min_y == 前腿外侧面 | −0.23（默认参数） | 0.010 |

- 复位放在 `try/finally`：任一检查失败也会先把被改的参数写回，最后统一抛错（退出码 2）。
- 上面的默认参数包围盒是**独立手算**并与"逐角点旋转再取极值"的暴力计算逐位一致：
  width 0.48、y ∈ [−0.23, **0.271818**]、z ∈ [0, **0.870148**]（θ = 0.10 rad）。
  这一组数字可直接用于对照真实 Blender 的求值结果。
- 预览：`blender_capture_preview(object=…, path="previews/parametric_chair.png", mode="render",
  view="isometric", width=960, height=720)`。第一次**故意用 `overwrite=false`**；只有被拒绝（文件已存在）
  时才用 `overwrite=true` 重试，并打印 `[warn]` 说明替换了旧预览。工具返回的 PNG 是 MCP image 内容，
  配方只打印元数据（路径/尺寸/字节数），不把 base64 灌进日志。

---

## 8. 输出与"不做的事"

- 保存：`blender_save_blend(relative_path="chairs/parametric_chair.blend", overwrite=false)`，
  落在 `<workspace>\Project\chairs\parametric_chair.blend`；addon 会自建父目录并临时关闭
  `save_version`，**不产生 `.blend1`**（`blender\blender_mcp_addon.py:2369`）。
  已存在且未给 `--overwrite-save` 时报错并提示显式覆盖，不静默替换。
- 本配方**不调用 `present_object`**（由 Codex 另行实现）。
- 除上表工具外，配方不写任何文件；`inspect` 阶段是纯只读。

---

## 9. 初版配方的离线自检（历史）

1. **语法编译**：`python -B -c "compile(open(...).read(), path, 'exec')"` → `compile-ok`（本版约 1660 行；`-B` 不落 `__pycache__`）。
2. **CLI 面**：`python -B examples\parametric_chair.py --help` → 7 个 phase、`--port/--suffix/--python/--timeout/--overwrite-save`。
3. **假客户端全阶段干跑**（不连接 Blender、不写文件；在内存里模拟 `create_node_group`/`batch_graph_edit`/
   `inspect_node_group`/`object_info`/`set_modifier_input`/`capture_preview`）：`inspect → seat → legs → back →
   finish → verify → save` 全部通过，结果：
   - `batch sizes: [21, 64, 100, 33]`，最大 100（≤ 限制），`transactional=true`；
   - 节点 65、链接 144、节点类型分布如上表；join 链接 1 → 5 → 10；
   - `verify` 9 项检查全 OK，参数复位为默认值（0.48 / 0.45 …），预览调用序列 `overwrite=[False, True]`；
   - 重跑安全：`seat` 拒绝（同名节点组已存在），`legs` / `back` 检测到已构建后 `[skip]`，不重复追加。
4. **算式独立核对**：`expected_bounds()` 与"包围盒角点暴力旋转取极值"逐位一致（0.271818 / 0.870148）。
5. 这次干跑**修正了两个真实缺陷**：join 链接累计数（back 后应为 10 而非 8）、以及 `max_y` 闭式解漏掉
   `(BackHeight/2)·sinθ` 项——后者若不带会误判正确的椅子，属于必须靠手算/联调才能发现的错误。

**配方编写阶段未做、现已由 Codex 完成**：真实 Blender 中创建节点组/批次提交、多输入 append 的真实链接累计、
`describe_node_type` 在本机 Blender 5.2 上返回的 socket 标识符、求值包围盒与 §7 预测值的吻合、
渲染预览耗时与首帧 GPU 初始化。

---

## 10. 架构警告与已知限制（逐条，不粉饰）

1. **采集器网格不可能真空**：`blender_create_mesh_object` 拒绝空顶点列表，所以用了 1 个原点顶点（无面）：
   `base_vertices = 1`，但求值几何完全来自节点组。若将来有"创建空网格"的具名工具，这里应改。
2. **无法用具名工具创建集合**：`Parametric Chairs` 不存在时对象落到场景集合，阶段打印 `[warn]` 并在结尾
   汇总，不假装用了该集合，也不擅自创建/删除集合。
3. **多输入 append 靠断言兜底**：MCP 的 `link_sockets` 只在 `replace=false` 时才拒绝"已链接的输入"，
   因此追加必须用默认 `replace=true`（Blender 对多输入插槽是累加）。配方在每阶段后核对真实链接数；
   一旦行为不符，阶段**失败并打印来源节点**，绝不静默生成缺件椅子。
4. **`back` 复用 `legs` 的偏移节点**（`Leg_X_Half` / `Leg_X_Neg` / `Leg_Y_Half`）：好处是偏移只有一处真源；
   代价是必须按 `seat → legs → back` 顺序执行，否则阶段报错提示补齐，而不是重复建节点。
5. **重跑语义**：`seat` 对同名节点组/对象**拒绝执行**（不删除、不替换）；`legs`/`back` 检测到对应部件已建时
   `[skip]` 并只做 `>=` 链接数核对，不会重复追加几何。
6. **预览覆盖策略**：默认先非破坏性尝试（`overwrite=false`），只有被拒绝后才显式覆盖并 `[warn]`。
   预览之外的任何文件都不会被配方触碰。
7. **受限沙箱下的 stdio 管道**：配方只用官方 `stdio_client`；某些受限环境会拒绝 anyio 创建管道
   （`PermissionError` / `WinError 5`），此时配方给出明确提示并指向测试套件使用的兼容传输
   （`tests\stdio_pipe_transport.py`），**不做隐式降级**。
8. **`Rail Height` 的语义**：定义为"第一根横档距座面的高度（沿柱轴）"，横档截面为 `LegSize × LegSize`，
   三根横档等分 `BackHeight − RailHeight`。三角形（座椅-靠背）都由此单一参数与 `BackHeight` 决定，
   没有隐藏的魔法系数；如需"横档更厚/更薄"，应新增参数而不是改常数。
9. **`Back Tilt` 上限 0.35 rad（约 20°）**：再大后柱会明显侵入座面布置，属于有意收窄的合理范围。
10. **`verify` 的模型一致性检查是硬失败**：它同时约束"图"和"预测式"。若真实 Blender 报不符，需要按
    §7 的数字判断是图的算式还是预测式的问题；配方打印 observed/predicted 两者供定位。

---

## 11. 复现命令汇总

```powershell
# 语法与 CLI（不连接 Blender、不写任何文件）
.\.venv\Scripts\python.exe -B -c "import pathlib; p=pathlib.Path('examples/parametric_chair.py'); compile(p.read_text(encoding='utf-8'), str(p), 'exec'); print('compile-ok')"
.\.venv\Scripts\python.exe -B examples\parametric_chair.py --help

# 真实联调（由 Codex 执行；需 Blender + 桥）
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase inspect
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase seat
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase legs
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase back
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase finish
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase verify
.\.venv\Scripts\python.exe examples\parametric_chair.py --phase save
```
