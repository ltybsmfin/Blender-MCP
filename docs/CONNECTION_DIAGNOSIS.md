# 连接诊断与安全启动

## 2026-10-04 本机故障证据

本次 Codex 会话可以调用原生 `blender_bridge_probe` / `blender_health`，因此 MCP 服务与注册已生效。
启动前探测显示 `reachable=false`、地址 `127.0.0.1:9876`、`token_present=true`；健康检查报
`bridge_unavailable` / Windows 10061（连接拒绝）。正常账户的进程清单没有 Blender，端口清单也没有监听。
这证明本次故障阶段是 **Blender 端 bridge 没有运行**，不能归因于 Codex 注册、版本或 token 不匹配。

随后通过具名工具启动一个后台诊断实例，参数为 `background=true, factory_startup=true,
extra_args=["--mcp-pump-seconds", "120"]`。PID 93284 健康检查报告 Blender **5.2.1 LTS**、协议 1、
地址 `127.0.0.1:9876`；当前会话的 `blender_health`、`blender_ping`、`blender_list_commands` 均成功。
日志 `MCP/.runtime/logs/blender_launch_1791104626.log` 包含 READY 和 120 秒后正常结束泵循环的记录。
诊断实例自行结束，没有关闭用户 Blender，也没有保存或打开用户工程。

进一步真实 GUI 验证定位到 **bootstrap 的实际启动缺陷**：脚本末尾执行 `SystemExit(main())`，
成功注册 timer 后 `main()` 返回 0，会让 Blender GUI 整体退出。修补为成功时正常返回，失败时抛出
明确异常；后台启动另带 `--python-exit-code 1`。修补前日志
`blender_launch_1791106358.log` 已出现 READY，但进程退出码为 0；修补后最终 GUI PID 94804
持续在线，原生 Codex health/ping/list_commands 与连续案例验收均成功。

另发现 Windows 临时 SDK stdio 客户端的 Job Object 会回收服务器的子进程，即使 Blender 使用
DETACHED_PROCESS。Windows 助手改为在自己的独立进程中调用同一 `AuditedFastMCP.call_tool`
具名入口（仍有 schema 校验、审计、桥鉴权），不创建这类短命 stdio 父服务，也不修改 SDK 回收规则。
最终启动日志 `blender_launch_1791106709.log`；助手结束后 GUI 桥仍在线。

端口、Blender 路径和 token 文件位置保持原配置。所有诊断只报告 token 文件存在性和路径，禁止输出内容。

## 调用顺序

1. `blender_bridge_probe()`：读取 `stage`、`error_code`、`address` 和 `hint`。
2. `stage=ready` 时直接调用 `blender_health`、`blender_ping`、`blender_list_commands`；无需启动。
3. `stage=connection` 时调用 `blender_launch_blender(reuse_existing=true)`。
   工具额外检查端口和 Blender 进程，只有两者都空闲才启动；已有健康桥直接复用。
4. 若提示 `blender_without_bridge`，用户在已开的 Blender 中运行 `MCP/blender/bootstrap.py`，
   或启用已安装的 Scene 属性页中的 Blender MCP Bridge。工具不会代替用户关闭或另开 Blender。
5. `authentication`、`token_file`、`response_timeout`、`protocol` 错误需要分别检查文件路径/权限、
   等待 Blender 当前工作完成、或核对 listener/add-on 版本；禁止为这些错误重复启动。
6. 启动失败返回 MCP `isError` / `bridge_start_failed`，包含 PID、exit_code、日志路径与最后阶段。
   先检查该 PID/日志并重试只读健康检查，不能因为观察超时就再次启动。

`probe` 的 `reachable=false` 是有意保留的诊断结果，并非建模成功；普通失败操作仍返回工具错误。
不自动重放场景修改，不关闭鉴权，不替换 token，不自动调整端口。

## 启动参数

`blender_launch_blender` 保留 `gui`、`background`、`factory_startup`、`reuse_existing`、`blender_path`。
`wait_seconds` 限定为 1–60 秒；不就绪时明确报错。`reuse_existing=false` 也不能绕过重复启动保护。
`extra_args` 只接受 `--mcp-quiet` 和 `--mcp-pump-seconds N`（有限正数）；禁止覆盖 host/port/token/project
或禁用 timer。后台模式自动开启泵；GUI 默认保留用户偏好，factory startup 仅用于受控验证。

Windows 助手通过官方 FastMCP 的类型化、具名且有审计的启动工具入口运行，避免另一套启动逻辑或 token 读取：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\AI\MCP\Blender\MCP\launch_blender.ps1
# 受控后台验证；仅在没有 Blender 实例时启动
powershell -NoProfile -ExecutionPolicy Bypass -File D:\AI\MCP\Blender\MCP\launch_blender.ps1 -Background -FactoryStartup -IsolateConfig -PumpSeconds 120
```

`-Force` 已禁用；`-ExtraArgs` 改用明确参数 `-PumpSeconds`。Python CLI 调用有整体超时，Windows
进程清单查询有 5 秒超时；查询失败会拒绝启动，不能把“不知道”解释为“没有进程”。

## 重连与源码更新

Blender bridge 自然退出/用户关闭 Blender 后，下一次请求重新连接同一地址；先探测，再按上面流程启动。
已运行的 Blender 不会自动加载磁盘上新修改的 add-on。完整 bridge 核心源码更新时，请先保留工程，由用户关闭旧实例后
重新启动；维护工具不强行终止用户进程。

本次新增的独立 `file_open.py` 命令模块提供了限定的刷新入口：在当前 Blender 的 Text Editor 打开并运行
`D:\AI\MCP\Blender\MCP\blender\bootstrap.py`。它仅重新加载并注册工程打开命令，保留已有 listener、timer、
token 和当前工程，不重启或关闭 Blender，也不重载其它模块。只重载此模块不代表完整 bridge 核心已更新。
此入口需执行磁盘上的新 bootstrap；已有控制台缓存的旧 bootstrap 模块不具有刷新逻辑。
MCP 工具 schema 在 stdio 服务启动时注册，Codex 重启/刷新服务后才能发现新增的场景工具。

针对性回归：`MCP/.venv/Scripts/python.exe MCP/tests/test_connection_startup.py`（无需 Blender，16 项测试）。


## 2026-10-05 算法复核：启动器与打开工程的边界

本次开始首例“两数相加”二轮检查时，health 与 bridge_probe 报告 127.0.0.1:9876 连接拒绝（WinError 10061，stage=connection，token_present=true）。最初把它当成必须由用户手动启动 Blender 的阻塞，没有先调用当前工具清单中已有的 blender_launch_blender；这是一次流程错误。用户提醒后，改用具名 MCP 工具启动，结果 launched=true、reused=false、PID 148160，Blender 5.2.1 LTS GUI，bridge_ready=true。没有结束已有进程，也没有通过桌面操作启动。

启动后 health、list_commands、list_scenes、list_objects 均成功。health 的 blend_filepath 为空；Scene 是默认启动场景，只有 Camera、Cube、Light，0 个 Geometry Node 组。它不是案例总工程。源文件仍未打开、未修改、未保存。

区分两个工具的职责：

- blender_launch_blender 在确认连接阶段为 connection 后，由工具检查已有 Blender/端口；只有安全时才启动。若已有 Blender 但没有 bridge，工具会拒绝，不要用脚本或其它方式绕过。
- blender_open_blend 已在实时工具清单中。它把 Project 内经校验的已保存 .blend 加载为当前 Blender 会话，不会启动第二个 Blender，也不会自动保存当前会话；必须显式传 confirm_replace_current=true。调用前先 health 核对当前文件、场景和会话状态。它成功后再读 health、命令清单确认实际 filepath，传输异常后绝不盲目重发打开命令。
- blender_inspect_blend_file 是只读元数据检查，不等于打开工程；本次在 bridge 未连接时调用仍返回 bridge_unavailable。此时不要据工具名称推断它可以离线读取。
- blender_inspect_blend_file 的 relative_path 是相对于 Project 根目录。传入 Project/算法复现/算法案例总工程.blend 会被拼成 Project/Project/算法复现/算法案例总工程.blend 并报 file_not_found；正确参数为 算法复现/算法案例总工程.blend。失败后核对错误中解析出的绝对路径，再修正一次，不要改源文件或尝试系统路径绕过。

Profile 也需要单独核实。本次会话启动时实际 MCP 工具清单含 90 项，插件配置未设置 BLENDER_MCP_PROFILE，因而使用默认 all。发现后已将本地插件配置设为 core；但本次已运行的 MCP server 仍是旧清单，必须重连后再验证 core 工具集已生效。项目文档中 core 提供工程打开/保存；algorithm_review 提供参数、节点树、模型检查和截图能力。按任务切换 profile 时重连/重启 MCP server，不要重启 Blender；每次重连重新核对工具清单和 Blender PID/path/scene。不能把“工具存在于 all 清单”描述成当前受限 profile 已启用，也不要在配置没有切换时声称已遵循 profile 隔离。

正确恢复顺序：health → bridge_probe → 仅当 stage=connection 时调用受保护 launcher → health/list_commands/scene/object 核验 → 用 core 打开源工程并另存唯一优化副本 → 切换 algorithm_review 后再读写目标 Geometry Nodes。若 launcher 因已有无 bridge 的 Blender 而拒绝，或 MCP profile 无法重连，保留源工程并报告具体状态；不要预先改节点、参数或源文件。
