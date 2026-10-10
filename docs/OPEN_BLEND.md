# 具名打开现有工程（`blender_open_blend` / 桥命令 `open_blend`）

状态：**已实现、已接入源码，60 项针对性单元检查与 28 项真实 MCP 验收通过**。
当前 Blender 5.2.1 LTS、PID 120772 的 bridge 已有 57 命令，包含 `open_blend`；
源码侧 MCP 列出 61 工具。真实验证已成功打开独立文件、修改节点产生实际几何变化并恢复检查点。
重开当前同一路径的磁盘副本预检已在同一实例实测通过。
当前检查点为 `_MCP打开验收_OpenVerify_8ad6d5e1/session_checkpoint.blend`，原场景 Scene 已恢复。

| 角色 | 文件 |
| --- | --- |
| MCP 工具（严格前置校验 + 有界只读核验） | `blender_mcp/tools/file_open.py` |
| Blender 侧桥命令模块（`register_commands(api)`） | `blender/file_open.py` |
| 单元证明 | `tests/test_file_open.py` |
| 本文档 | `docs/OPEN_BLEND.md` |

## 0. 源码接入与当前 Blender 更新

主 addon 已通过 `_register_file_commands()` 注册独立文件命令模块；
`blender_mcp/tools/__init__.py` 已在 `build_server()` 注册 MCP 工具。
既有 schema 回归已更新总数为 61，其余工具的参数和默认值保持兼容。

对于已运行的旧 bridge，在**当前 Blender 的文本编辑器**打开磁盘上的
`D:\AI\MCP\Blender\MCP\blender\bootstrap.py`，执行“运行脚本”。
这次入口只重新加载独立 `file_open` 模块、注册命令并复用现有健康连接；
不停止监听或计时器，不关闭 Blender，不打开、保存或替换任何工程。
已有 Text 数据块可能仍含旧脚本，应从磁盘重新打开文件。
该步骤是插件维护入口，不用于绕过具名 MCP 执行建模或文件加载。

入口现在直接复用本进程内正在运行的 bridge，不在主线程等待自身 TCP 回复；
本进程无 bridge 时再探测其它实例的连接。需要启动时才注册 UI 类，避免重复
`register_class` 引发 Blender 5.2 的 `ValueError`。旧入口曾在新命令注册成功后报错，
因此报错不能单独证明新命令未加载；应实际查询连接与命令清单。

执行后必须通过 `blender_list_commands` 确认出现 `open_blend`，再进行真实验收。
入口刷新回归 `tests/test_file_bootstrap.py` 三项通过，包括禁止主线程自我探测、
重复注册错误注入及配置不符时保留连接。该最后入口分支通过模拟检查，
未再次要求用户人工运行，不把模拟检查表述为人工界面验收。

`open_mainfile` 会替换 Main 数据库；源码现有计时器设置为 `persistent=True`，
GUI 中连接、窗口与命令在真实加载后存续已实测；后台模式未实测。
`tests/test_open_blend_acceptance.py` 只通过具名 MCP 使用 Project 内独有验证文件，
检查两种 `load_ui` 模式、节点读写、路径保护、目标文件不被改写，并恢复专用会话检查点。
检查点由单独的显式保存工具创建，恢复后当前文件路径为检查点路径；
打开工具本身不隐式保存当前工程。

重开当前磁盘文件时，Blender 拒绝 `libraries.load` 枚举当前 Main 的同名文件。
新预检将磁盘内容复制到 `MCP/.runtime/open-preflight-*.blend` 的唯一临时文件，
只枚举该副本，随后删除副本；检查复制前后源文件的大小、时间与文件标识未变。
不会把未保存的当前 Main 写入副本，不修改目标，正式打开仍使用原目标路径。
`preflight.inspection_mode` 区分 `direct` 与 `temporary_disk_copy`；源文件在复制过程中
发生变化时拒绝并返回 `source_changed`。这里的只读指源文件和当前 Blender 数据；
临时副本会写入 MCP 运行目录。

## 1. 工具签名与参数

```
blender_open_blend(path: str,
                   confirm_replace_current: bool = False,
                   load_ui: bool = False) -> dict
```

| 参数 | 类型/默认 | 说明 |
| --- | --- | --- |
| `path` | `string`，必填 | **规范化绝对** `.blend` 路径，且必须位于 `<workspace>/Project` 内 |
| `confirm_replace_current` | `boolean`，默认 `false` | 必须传 `true`；打开会替换当前整个会话文件 |
| `load_ui` | `boolean`，默认 `false` | `false`=保留当前窗口布局；`true`=让 Blender 恢复文件里保存的界面布局 |

两个布尔是 **strict boolean**：JSON 布尔以外的值（字符串 `"true"`、数字 `1`/`0`、
`null`）在 schema 校验阶段即被拒绝，不会被静默强转。

返回（成功时）在桥命令负载之上增加 `verification` 块：
```json
{
  "opened": true,
  "path": "D:\\AI\\MCP\\Blender\\Project\\demo\\case.blend",
  "previous": {"blend_filepath": "...", "scene": "...", "is_dirty": true, "is_dirty_known": true},
  "current": {"blend_filepath": "...", "scene": "Case", "scenes": ["Case"], "objects_total": 12,
               "is_dirty": false, "window_count": 1, "background": false},
  "operator": "wm.open_mainfile",
  "exec_context": "EXEC_DEFAULT",
  "load_ui": false,
  "use_scripts": false,
  "confirmed_replace_current": true,
  "saved_before_open": false,
  "target_written": false,
  "pid": 11677,
  "blender_version": "5.2.1",
  "ui_mode": {"background": false, "load_ui": false, "window_count": 1},
  "preflight": {"read_only": true, "container": {...}, "counts": {...}, "sample": {...},
                 "live_state_unchanged": true},
  "rollback": "not_attempted",
  "verification": {"verified": true, "mode": "reply", "attempts": 1,
                    "path_matches": true, "pid_matches": true, "open_blend_listed": true,
                    "open_blend_replayed": false, "commands_retried": ["health", "list_commands"]}
}
```

若回复在传输中丢失但证据齐备，返回的是**较窄但仍然完整**的 `post_hoc` 形状：
`opened`、`path`、`current_filepath`、`current_scene`、`pid`、`blender_version`、`load_ui`、
`use_scripts=false`、`ui_mode`、`verified_after_transport_failure=true`、
`safety_options_confirmed_by_evidence`、`transport_error`，以及
`previous=null` + `previous_unavailable`（回复丢失，未观测到开前状态）——不猜测未观测到的值。

## 2. 与 `blender_inspect_blend_file` 的区别

| | `blender_inspect_blend_file` | `blender_open_blend` |
| --- | --- | --- |
| 是否改变当前会话 | **否**，只读 | **是**，替换整个已加载文件 |
| 读取方式 | `bpy.data.temp_data()` + `libraries.load` 仅枚举 | 先做同样的只读枚举证明可解析，再执行 `wm.open_mainfile` |
| 目标格式 | `relative_path`（相对 Project） | `path`（**绝对** Project 内路径） |
| 确认 | 不需要 | **必须** `confirm_replace_current=true` |
| 典型用途 | 导出后校验 case 文件内容 | 把某个 case 文件重新载入，继续修改 |

先 `inspect` 再 `open` 是推荐顺序：`inspect` 证明文件可枚举，`open` 前后都做路径/
可读性/可解析证明。

## 3. 路径规则（两层预验，两层规则一致）

MCP 侧（`blender_mcp/tools/file_open.py`，**在联系 Blender 之前**）与 Blender 侧
（`blender/file_open.py`，**在替换任何东西之前**）各校验一次，且都复用既有的安全路径
实现（MCP 侧 `config.resolve_project_path`，Blender 侧 `api.resolve_project_path`）：

* 必须是 `str`，非空，无 NUL，长度 ≤ 4096；
* 必须是**绝对**路径（相对路径拒绝），且必须同时具备 **root 与 drive**（Windows）：
  `\dir`（rooted 但无 drive，`os.path.isabs` 会误判为绝对）与 `C:foo`
  （drive-relative，依赖每盘 cwd）一律拒绝；
* 必须是**规范化**路径：`.`、`..`、重复分隔符、结尾分隔符一律拒绝
  （`/` 与 `\` 混用会被规范化后接受）；
* 拒绝 UNC（`\\server\share\...`）与设备路径（`\\?\...`、`\\.\...`）；
* 必须位于 `<workspace>/Project` 内（先做包含性检查，再经既有 helper 复核 realpath）；
  **跨卷/跨盘**（`os.path.relpath` 在 Windows 上会抛 `ValueError`）被结构化捕获并报
  `path_outside_project`（`details.reason="different_drive"`），不是内部错误；
* 拒绝 symlink/junction/reparse 组件（沿用既有规则的检查，未另写一份）；
* 必须是已存在的**普通文件**、后缀为 `.blend`（大小写不敏感）、且可读（真实打开读 1 字节）；
* 绝不为了“能打开”而创建不存在的目录或文件——目标文件不存在直接 `file_not_found`。

示例（假设 Project 根为 `D:\AI\MCP\Blender\Project`）：

```
blender_open_blend(path="D:\\AI\\MCP\\Blender\\Project\\demo\\case.blend",
                   confirm_replace_current=True)
blender_open_blend(path="D:/AI/MCP/Blender/Project/demo/case.blend",   # 分隔符会规范化
                   confirm_replace_current=True, load_ui=True)
# 会被拒绝：相对路径 / UNC / 设备路径 / .. / 根之外 / symlink / 目录 / 非 .blend / 不可读
```

**为什么必须绝对路径**：打开是破坏性操作，相对路径的解释依赖调用方进程的工作目录，
两个进程（MCP server 与 Blender）的 cwd 可能不同。规范化为绝对路径后再传给 Blender，
桥两端比较的永远是同一条规范化字符串，不会出现“我以为是这个文件”。

## 4. 为什么总是要求 `confirm_replace_current=true`

* `bpy.ops.wm.open_mainfile` **整体替换**当前 main 数据库：当前文件、场景、选中、
  未保存的一切都会消失，且**本工具不隐式保存**（不会替你 `save`，也不会写目标文件）。
* `bpy.data.is_dirty` 只是**提示**：通过直接 RNA/`bpy.data` API 做的编辑不一定会置位
  该标志，因此它不能证明“所有改动都已落盘”。所以确认不能由 `is_dirty` 代替。
* 因此：`confirm_replace_current` 不是 `true` 时
  * 会话 dirty（`is_dirty is True`）→ 错误码 `unsaved_changes`；
  * 会话 clean 或 `is_dirty` 未知（缺失/非布尔）→ 错误码 `confirmation_required`。
  两种情况 `details` 都含 `current_filepath`、`is_dirty`、`is_dirty_known`、
  `would_replace_current_file` 与替换告警。
* 确认检查发生在**任何读取/枚举/加载之前**，被拒绝时不会读目标文件、不会调用 operator、
  不会修改任何数据。

## 5. 脚本安全与界面

* **脚本始终关闭**：只调用正式 RNA `bpy.ops.wm.open_mainfile`，显式使用
  `EXEC_DEFAULT` 上下文，并硬编码 `use_scripts=False`；工具**不暴露**任何可执行脚本的
  开关。
* 调用前用 `operator.get_rna_type()` 核实该 build 的 `wm.open_mainfile` 真的提供
  `filepath`(STRING)、`load_ui`(BOOLEAN)、`use_scripts`(BOOLEAN)；缺任何一个或类型不符
  → `unsupported_api` 并中止（**不猜参数**）。
* `load_ui` 默认 `false` → 保留当前窗口布局；`true` 时才请求恢复文件保存的界面布局。
* 使用**当前实例**，不新开、不关闭 Blender，不写目标文件。

## 6. 成功之后的只读核验（绝不重放写请求）

`open_blend` 返回成功后，工具用**只读**命令核验，总预算 `VERIFY_BUDGET_SECONDS`（常量，
默认 10s；单次调用 `VERIFY_CALL_TIMEOUT` 默认 3s，间隔 0.25s）：

1. `health` → `blend_filepath` 必须等于请求的目标，`pid` 必须等于 `open_blend` 回复里的 pid；
2. `list_commands` → 命令名单必须含 `open_blend`。

预算语义（不夸大）：**每次读之前**先算 `remaining`，`remaining <= 0` 时**不再发起任何 RPC**，
每次读的超时是 `min(VERIFY_CALL_TIMEOUT, remaining)`，因此读请求不会越过预算；但
`bridge.call` 内部**新建 TCP 连接**仍受 `config.CONNECT_TIMEOUT`（`bridge.py` 未改动）约束，
该连接建立预算另计，所以墙钟时间可能略超预算——这是一个**读请求上限**，不是绝对墙钟封顶。
`health` 之后预算刚好耗尽时不会再去读 `list_commands`，本次自然判为未通过核验。

三次证据齐备才算成功，否则 `isError` + `verification_failed`（`details` 带 `stage`、
`path_matches`、`pid_matches`、`open_blend_listed`、`attempts`、`reads_issued`、
`reads_skipped_for_deadline`、`deadline_exhausted` 等）。重试**只**针对这两个读命令，允许
`bridge.close()` 后重连；**绝不**重新发送 `open_blend`，`BridgeClient` 本身不增加任何通用重试。

**回复在传输中丢失（超时/断连）时**：仍然做同样的只读核验，但**同路径不算证据**。证据必须是
**Blender 侧写入的** `command_end`（`source == "bridge"`）、**本调用的 request id**、且记录的
结果自身确认了 `opened=true`、目标路径一致、`use_scripts=false` 与所请求的 `load_ui`；其它
source、仅路径相同、或缺少安全选项确认的记录都不算证据。判定：

* 证据齐备且只读核验通过 → 成功（`verified_after_transport_failure=true`，
  `verification.mode="post_hoc"`），返回**健康观测到的** `current_filepath`、`pid`、
  `blender_version`、`current_scene`（`background`/`load_ui` 在 `ui_mode`），加上**原请求**的
  `load_ui` 与证据确认的 `use_scripts=false`；`previous=null` 并在
  `previous_unavailable` 说明“回复丢失，未观测到开前状态”；`safety_options_confirmed_by_evidence`
  给出证据侧确认明细；
* 有该请求的失败记录 → `load_failed`；
* 路径已匹配但缺少可用证据 → `load_pending`；其余 → `verification_failed`。

三种失败情况都是 `isError`，不会伪报成功。

请求关联依赖 add-on 在 `health` 中声明 `audit_protocol`（MCP 侧仅在此时把 request id 注入
请求）。若线上 add-on 过旧/未声明，关联不可用：此时“回复丢失”一律按无证据处理
（`load_pending` 或 `verification_failed`），绝不因路径相同而报成功。

## 7. 错误码

MCP 侧（未联系 Blender 或联系失败）：

| code | 阶段 | 含义 |
| --- | --- | --- |
| `invalid_argument` | 参数 | `path` 非字符串 / 布尔非 strict bool |
| `invalid_path` | 路径 | 空、NUL、相对、未规范化、后缀不是 `.blend` |
| `unc_path` / `device_path` | 路径 | UNC / 设备路径 |
| `path_outside_project` | 路径 | 解析后逃出 Project、跨卷/跨盘、或含 symlink/junction 组件 |
| `invalid_project_dir` | 路径 | Project 根本身不合法（override/链接） |
| `file_not_found` / `not_a_file` / `unreadable` | 路径 | 不存在 / 非普通文件 / 不可读 |
| `verification_failed` | 核验 | 回复称成功但只读核验不一致 |
| `load_pending` | 核验 | 回复丢失、路径已匹配但无请求关联证据 |
| `load_failed` | 执行 | 桥端审计显示该请求失败 |

Blender 侧（`BlenderCommandError`，`isError` 文本为
`<code>: <message> | details: {...} | stage: <阶段>`）：

| code | 阶段 | 含义 |
| --- | --- | --- |
| `invalid_argument` | 参数 | 布尔/路径类型错误 |
| `invalid_path` / `unc_path` / `device_path` / `path_outside_project` | 路径 | 同上的第二层 |
| `file_not_found` / `not_a_file` / `unreadable` | 路径 | 同上的第二层 |
| `unsaved_changes` | 确认 | 未确认且 `is_dirty` 为真 |
| `confirmation_required` | 确认 | 未确认且 clean/未知 |
| `unsupported_api` | API/预检 | `wm.open_mainfile` RNA 或只读枚举 API 不可用 |
| `invalid_blend` | 预检 | 容器魔数不可识别（损坏/非 .blend 内容） |
| `blend_parse_failed` | 预检 | 魔数可识别但 Blender 读不出 ID 清单 |
| `preflight_changed_state` | 预检 | 只读预检竟改动了 live 状态（自我保护） |
| `load_failed` | 加载 | `details.stage` ∈ `exec` / `operator_result` / `verify_filepath` / `verify_scene`；`details` 诚实给出 `current_filepath`、`current_filepath_changed`、`state`、`rollback="not_attempted"` |

**不承诺任何数据回滚**：加载失败或被拒绝时，会话停在当下真实状态，错误细节如实描述。

## 8. 与 case 工作流的衔接

```
blender_create_case_scene(name="Case42")      # 全新空场景
  → 建模（geometry nodes / mesh / shape keys / drivers）
  → blender_save_scene(scene="Case42", relative_path="cases/case42.blend")
  → blender_inspect_blend_file(relative_path="cases/case42.blend")   # 只读校验导出物
  → blender_open_blend(path="<Project>\\cases\\case42.blend",        # 真正载入
                        confirm_replace_current=True)                # 先保存/确认放弃当前工作
  → 继续修改；需要保存时再显式 blender_save_scene / blender_save_blend
```

注意：`open_blend` 之后当前 main 文件就是该 case 文件，后续保存遵循各保存命令自身的
规则（`save_scene` 仍不重指 main 文件；`save_blend` 仍整体保存、`overwrite` 默认 false）。

## 9. 单元证明（本次可复现）

命令（工作目录 `D:\AI\MCP\Blender\MCP`，`timeoutMs` ≤ 60000）：

```
.\.venv\Scripts\python.exe tests\test_file_open.py
```

结果：`Ran 60 tests` → `OK`（exit 0，约 5s）。覆盖：

* 新工具字段默认值（`path` 必填 string、两个布尔 `default=false`、`type=boolean`）与
  strict boolean 拒绝 `"true"`/`1`/`0`/`null`；
* 既有 **60** 个工具 schema 与基线逐一相等、新工具只新增 1 个 name（60 → 61）；
* 未 import `bpy`、未改动共享 helper（`config.resolve_project_path` / `project_dir` 同一对象）；
* 旧只读 `blender_inspect_blend_file` 行为不变（仍先拒绝 `..`，仍只发 `inspect_blend_file`）；
* 路径边界两层预验（空/相对/UNC/设备/NUL/未规范化/Project 外/`.`+`..`/结尾分隔符/
  不存在/目录/非 `.blend`/不可读/reparse 组件/**跨盘**/**rooted-no-drive**/**drive-relative**），
  且被拒绝时**零**桥流量；`_absolute_syntax_ok` 的 Windows 与 POSIX 两套规则各有一条单测
  （显式 `windows=` 参数，非 Windows fixture 同样可测）；
* 有效路径规范化后原样转发（`confirm_replace_current`/`load_ui` 严格透传）；
* Blender 侧：损坏 `.blend` 容器 → `invalid_blend`；可识别但不可解析 → `blend_parse_failed`；
  布尔严格校验；dirty/clean/unknown 三种确认结果；`wm.open_mainfile` RNA 缺失/类型不符 →
  `unsupported_api` 且不调用 operator；`FINISHED` 成功负载（含 `use_scripts=false`、
  `EXEC_DEFAULT` 参数、`previous`/`current` 快照、`preflight.proved_by`/`compared`）；
  非 `FINISHED` → `load_failed`；operator 抛异常 → `load_failed` + 诚实 details；实际路径不符 →
  `verify_filepath`；**未命名会话 `'' → ''` 不报路径变化**、`'' →` 已命名报变化；
  预检漂移（`is_dirty` 翻转、或只有完整指纹才看得见的 `save_version` 变化）→
  `preflight_changed_state` 且 operator 未被调用；
* MCP 侧：回复 + health + 命令名单三者一致才算成功；health 掉线一次后恢复（`attempts=2`）；
  一直失败/名单缺 `open_blend`/pid 错/路径错 → `verification_failed`；回复命名了别的文件 →
  在核验前即拒绝；**假时钟**证明“发起读之前先检查 deadline、每次读超时 ≤ 剩余预算、deadline
  之后不再发起读”（同一断言对旧循环形状会失败：旧实现第 41 次读发生在 deadline 时刻且
  超时 3.0s > 剩余 0s）；
* 传输超时分支：`source=bridge` + request id + `opened/path/use_scripts=false/load_ui` 全部
  确认 → 成功（`post_hoc`），且返回 health 观测的 `current_filepath`/`pid`/`blender_version`/
  `current_scene`、请求的 `load_ui`、`use_scripts=false`、`previous=null`；source 不是 bridge、
  缺 `use_scripts`、`use_scripts=true`、缺 `opened`、路径不符、`load_ui` 与请求不符、request id
  不同 → `load_pending`；失败记录 → `load_failed`；**`open_blend` 始终只被发送一次**。

真实 GUI 验收：`tests/test_open_blend_acceptance.py` 的 28 项检查通过，记录在
`MCP/.runtime/open_blend_acceptance.json`。确认相同 PID 中打开专用文件、两种 load_ui 模式、
重开当前文件的副本预检、节点修改后实际三角形范围变化、文件哈希不变、检查点恢复
及原场景对象完整属性/节点图一致。默认未确认、无效路径、缺文件、损坏文件实测拒绝。
失联恢复及失败状态使用单元模拟证据，不故意破坏用户进程；后台模式与大文件耗时未测。

## 10. 实现注意事项 / 已知限制

* 只读预检复用既有 `_blend_container` + `_temp_blend_metadata`（`bpy.data.temp_data()` +
  `libraries.load` 仅枚举、忽略 `data_to`），不把任何 datablock 载入 live `bpy.data`；预检
  前后比较 **包含 `is_dirty` 在内**的 live 状态，且优先复用 add-on 的完整
  `_live_state_fingerprint()`（存在时）以覆盖 objects/scenes/windows/save_version 等更多维度；
  任何漂移 → `preflight_changed_state` 并**不调用** operator。返回里的
  `preflight.live_state_unchanged=true` 同时带 `proved_by`（用的是
  `api._live_state_fingerprint` 还是本地标量快照）与 `compared`（实际比较过的字段），不使用
  完整指纹时**不夸大**证明范围。注意：预检若翻转 `is_dirty` 也会被判为漂移并 fail-closed
  拒绝打开（宁可拒绝，不隐瞒可观测变化）。
* 若某 build 不提供 `temp_data`/`libraries.load`，打开被**拒绝**（`unsupported_api`），不会
  退化成“只看后缀”。
* 快照只保存字符串/标量（路径、场景名、计数、布尔），不持久保存任何 RNA 对象引用。
* `load_failed` 的 `details.current_filepath_changed` 只在**两个已知路径确实不同**时为真：
  `''` 是 Blender 的未命名会话，`'' → ''`（即使 operator 抛异常或返回非 `FINISHED`）报告
  `false`，未命名 → 已命名报告 `true`，不会把“未改变”说成改变。
* `open_blend` 的调用超时是 `OPEN_CALL_TIMEOUT`（默认 120s，有界）；大文件加载会阻塞
  Blender 主线程，期间桥不响应——这是超时存在的原因，也是“回复可能丢失”的来源。
* 成功后主文件已改变，工具**不**替调用方重新 `health` 之外的任何状态；`verification` 只
  证明“就是这次调用加载了这个路径”。
