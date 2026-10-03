# Blender MCP 架构与建模约定

## 目标

让本地 Codex 通过 MCP 操作 Blender，以几何节点构建可编辑的参数化模型，并能够使用形态键和驱动器。

用户明确要求不使用 computer-use / 桌面自动化。操作与验收通过具名 MCP 工具和 Blender API 完成；模型预览通过 `blender_capture_preview` 返回图片，不依赖桌面截图。

## 组件

1. Codex 以标准输入/输出启动外部 MCP 服务。
2. MCP 服务负责工具定义、参数校验和本地桥接请求。
3. Blender 桥接组件在主线程调用 `bpy`，返回实际场景与求值结果。
4. 相对于工作区 `D:/AI/MCP/Blender`，`.blend` 示例和用户输出放在 `Project/`；不同版本 Blender 程序放在 `Version/`，当前版本为 `Version/5_2_1_LTS/`。

MCP 项目根目录及 Git 根目录均为 `D:/AI/MCP/Blender/MCP`。源码、安装环境、项目文档、日志和测试均位于此目录。`Version/` 与 `Project/` 是它的同级目录，不属于 MCP Git 仓库。

不在 Blender 的内置 Python 中安装 MCP SDK，也不编译或修改 Blender 源码。

## 参数化建模流程

- 先读取现有对象、节点组接口和实际节点插槽，再编辑。
- 优先把可调整尺寸、数量和形态暴露为几何节点组的输入参数。
- 保留节点组和修改器；保存前检查求值后的几何，不能只检查创建调用是否返回成功。
- 插槽应通过唯一标识或明确索引寻址；同名插槽存在歧义时应报错。
- 批量编辑若不是事务，应明确报告已经完成的操作，不声称自动回滚。

## 形态键与驱动器

形态键依赖原始网格的顶点拓扑。几何节点若从程序化基本体生成全新几何，原始网格的形态键不一定影响该输出；应通过节点参数、保持原网格流经节点的设计或分开的对象来表达对应变化。

驱动器应明确记录目标数据块、数据路径、变量和表达式，并通过改变源属性后检查目标值来验证。仅成功创建驱动器不足以证明依赖关系正确。

## 验证与维护

- 用真实 Blender 验证节点参数、形态键影响和驱动器求值。
- 用标准 MCP 客户端验证初始化、工具发现和调用，以及错误返回。
- 启动失败、错误数据路径、歧义插槽、形态键拓扑不匹配和保存覆盖应有明确反馈。
- 不把 Blender 分发文件、虚拟环境、本地凭据或运行产物提交到 Git。
- 具体工具与运行方法以 MCP 项目根目录 `README.md` 和实现为准；本文件描述设计约定。

---

## 审计日志与实时看板（本版新增）

### 数据流

```
Codex ──stdio──> FastMCP(AuditedFastMCP.call_tool) ──审计──> .runtime\logs\audit-mcp-<pid>-<session>.jsonl
                        │
                        └── TCP 桥（参数里带 _mcp_audit.request_id）──> Blender dispatch ──审计──>
                                                                        .runtime\logs\audit-bridge-<pid>-<session>.jsonl
看板（log_viewer，仅 127.0.0.1）──只读聚合 audit-*.jsonl──> 浏览器页面（轮询 /api/events、可选 SSE）
```

### 关键约定

1. **每次调用都记录**：`tool_start`（工具名 + 已脱敏参数 + `status=running`）与 `tool_end`（结果摘要、错误、耗时）。
   Blender 侧 `dispatch` 对每个桥命令记录 `command_start` / `command_end`（含 `validation` 标记：未知命令、
   未知/缺失参数在**改数据之前**就被拒绝）。批量编辑额外记录 `batch_step`（逐步）与 `batch_rollback`（事务结论）。
2. **同一请求号贯穿两侧**：MCP 侧生成 `request_id`，桥请求参数里携带保留字段 `_mcp_audit`；
   Blender 的 `dispatch` 在校验参数**之前**把它摘掉，因此它永远不会被当成命令参数。
   运行时 Blender 只有在 `health` 里声明 `audit_protocol>=1` 时才会收到该字段（旧插件不受影响）。
3. **绝不泄密**：键名含 token/secret/password/authorization/… 的值写 `***`；真实 token 文本会被替换；
   `token_present` / `token_path` 这类“只是提到”的键保留。base64/bytes 只记 `_type=binary` + 长度 + sha256。
4. **有界**：字符串 320 字符、数组前 20 项 + `sha256`（>200k 项只记长度）、对象最多 64 键、嵌套 5 层。
   图片结论：看板只显示字节数与哈希，**不显示 base64**。
5. **多进程安全**：每进程一个文件（`audit-<source>-<pid>-<session>.jsonl`），不共享写句柄；
   按大小轮转（默认 2 MiB）为 `<name>.NNN.jsonl` 并保留 5 个归档。看板按“文件路径 + 偏移”读取，
   文件变小即视为轮转，重新从头读，因此**轮转期间不丢事件**。
6. **日志失败不影响建模**：任何写入异常只让本次事件丢失，并在 stderr 打一条 30 秒限频的告警（stdout 只属于 MCP 协议）。
7. **看板面**：仅 `GET /`、`/api/health`、`/api/events`、`/api/stream`（写请求 405，未知路径 404），
   只绑定回环并二次校验对端地址；目录在构造时固定，**任何查询参数都不能指定文件路径**；
   启动用端口 0（由系统分配）并把真实 URL 原子写入 `viewer-<pid>.json`，因此并发启动互不冲突，显式占用的端口会明确报错。

## 事务化批量图编辑

- 默认 `transactional=true`：先 `tree.copy()` 出隔离草稿，在草稿上执行全部操作；任何一步失败就删掉草稿，
  原节点组**一个字节都没动**（返回 `rolled_back`，并由 MCP 工具转成错误）。
- 全部成功才提交：把草稿提升为原名、把原名让给草稿，然后**重定向已枚举的使用者**：
  修改器的 `node_group`、其它节点组里的 Group 节点、驱动器变量目标（`DriverTarget.id`）。
  重定向前后分别给属主 ID 打 tag，保证修改器与驱动器重新求值；修改器输入值按 identifier 快照后回写。
- 提交前检查 `original.users == 0`：如果还有**无法枚举**的引用，则整体回退并报 `unremappable_references`，
  绝不留悬空指针。链接库/库覆盖节点组直接拒绝（`unsafe_linked_group`）。
- 诚实边界：成功提交会**替换节点组数据块身份**（名称、接口 identifier、修改器参数不变）；
  这不是 Blend 文件级撤销，bpy 之外缓存旧数据块的代码不会被改写。草稿与旧数据块都会被清理，不留 `.__mcp_` 垃圾。

## 截图预览

- `mode=auto`：有真实 3D 视口（GUI 且非 `--background`）走 `render.opengl` 视口截图，否则走临时场景相机渲染，
  并在响应里回报**实际**模式；`mode=viewport` 在无 GUI 时返回可执行的 `viewport_unavailable`。
- 取景用**求值后**几何（几何节点输出 + 依赖图实例）的世界包围盒；相机**投影拟合**：
  8 个角 → 相机空间，`sensor_fit='HORIZONTAL'` 下
  `ortho_scale = max(投影宽, 投影高 × 宽/高) × 1.12`，裁剪面由投影深度决定。相机矩阵由
  `location + rotation_euler` 直接构造——临时场景中的相机不会被当前视图层的 `update()` 刷新
  `matrix_world`，读它会得到错误的拟合/裁剪面（模型被裁掉或画面空白）。
- 渲染在**临时场景**里进行，并从源场景复制帧、子帧、fps/fps_base、起止帧与场景自定义属性，
  使 GN/驱动器/动画在渲染时与元数据完全一致；不动用户场景的渲染设置/相机/帧，临时相机、灯光、世界、
  场景在 `finally` 里删除。视口截图还会快照并恢复**选中集、活动对象**、子帧与 fps。
- 纯实例几何（GN 只输出实例、求值网格为空）从 `depsgraph.object_instances` 取点计算包围盒（有上限与
  截断标记）；仍无法测量时给出 `code_hint="realize_instances"` 的可执行提示。
- 输出目录固定为 `Project\previews`，路径双重校验；渲染先写 `.runtime\tmp`（与目标同一卷），
  校验 PNG 头与 3 MiB / 2048 px 上限后用 **`os.replace` 原子替换**（不预先删除旧文件，失败时旧图保留）。
- 返回 **MCP image 内容**（`image/png`）+ JSON 元数据；服务端再校验返回路径仍在 `Project`、尺寸与字节上限。

## 审计计数：事件与操作

- `tool_start`/`command_start` 与 `tool_end`/`command_end` 按「来源 + 会话 + 进程 + 请求号 + 命令名」配对。
  一个配对、一个孤立 end、`bridge_ok/bridge_error` 各算 **1 个操作**；
  `batch_step`/`batch_rollback`/`session_*`/`bridge_audit_*` 只作信息行，不进入四个状态计数。
- **只有仍在活动会话中、且没有对应 end 的 start 才算 `running`**；会话已 `session_stop` 时，未闭合的
  start 记为 `info`（历史），因此看板的"进行中"不会等于历史 start 数。
- 看板为此读入 1500 条事件窗口（展示切片仍受 `limit` 约束），并把每行标注
  `display_status`（`completed`/`running`/`info`/`ok`/`error`/`rolled_back`）供筛选与着色。
- 页面按行 key 复用 DOM：内容未变化的行不重写，展开状态与滚动位置在 1.5 秒轮询之间保持不变
  （`follow` 勾选时才贴底滚动）。

## 关联能力的恢复

- 桥包装器只在对端声明 `audit_protocol>=1` 时注入保留字段 `_mcp_audit`。
- 首次探测失败（例如 Blender 还没起）会记录 `bridge_audit_unavailable`；之后**重新武装**：
  传输类失败（`bridge_unavailable`/`bridge_timeout`/`bridge_protocol_error`）后重试探测，
  或任意一次 `health` 回答中出现 `audit_protocol` 时立即恢复，并记录 `bridge_audit_regained`。
- 整个过程中**不重放任何请求**，只影响后续请求的关联字段。
