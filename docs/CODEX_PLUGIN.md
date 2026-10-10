# Codex 本地插件接入

## 当前：工具常驻，域只用于文档查询（2026-10-05）

用户澄清后，服务在所有 profile 下始终提供全部93项工具。
`blender_tool_domains(domains=["geometry_nodes"])` 按界面返回说明和参数；
目录选择不会禁用其他工具，亦无需工具列表通知或每次重连。

插件配置里的 BLENDER_MCP_PROFILE=global-only 现在只选初始通用文档视图，可以保留。
实际安装的同一插件已通过新的独立 Codex app-server 连接确认93项，
含三个目录接口，tools.listChanged=false。证据：`.runtime/codex_tool_directory_status.json`。
这证明新连接能正确加载当前程序，不等同于当前桌面聊天的旧进程已自动替换。

已运行旧 server.py 的会话需一次 MCP 重连装入新代码；无需重启 Blender 或运行 bootstrap。
此后按界面查询不会要求重连。无需为浏览不同界面反复改配置或重装插件。
详见 [工具目录](TOOL_PROFILES.md) 和 [验收](TOOL_DIRECTORY_ACCEPTANCE.md)。

以下 core20、all90、旧动态global25 等记录属于历史阶段，不是当前工具可用性约定。
源清单与安装缓存独立这一排障经验仍适用，但旧数量及 profile 权限解释已被本次修正替代。

## 历史：core20配置缓存故障与修复（2026-10-05）

本地插件源配置 `codex-marketplace/plugins/blender-mcp/.mcp.json` 已显式设置
`BLENDER_MCP_PROFILE=core`，本机安装缓存现已同步。当时 core 工具清单为20项；当前所有profile均93项。

插件源文件与 Codex 已安装缓存是两份文件。修改源文件后，仅重启 Codex 不会把修改
重新安装到缓存中。本次故障中，源文件有 core，缓存缺少该变量，所以重启仍获得默认 all
的 90 项工具；原生 health 成功且新增命令存在，说明连接和新代码已经可用。

更新源配置后，在本机重新安装同一个插件即可同步配置，无需卸载、删除缓存或新建重复 MCP：

```powershell
codex plugin add blender-mcp@blender-local --json
codex mcp get blender --json
```

在本机 Codex 0.160.0，上述 add 成功刷新原有 0.1.0 安装目录。
核验 get 返回的 `transport.env.BLENDER_MCP_PROFILE` 必须是 core，不能仅检查项目源文件。
然后重新连接 MCP，或完整退出并重新打开 Codex，才能使已有会话使用新的启动配置。
无需重启 Blender、运行 bootstrap 或保存工程。固定维护工具只刷新 Blender 端模块，不能改变
Codex stdio 进程的 profile 或已有聊天的工具清单。

本次实际验证：更新前当前聊天原生清单 90 项，原生 health 成功，Blender 5.2.1 LTS/PID154996；
更新后缓存和 `codex mcp get` 均返回 core，独立 Codex app-server 的
`mcpServerStatus/list` 返回同一插件 `blender-mcp@blender-local`、20 项工具及 health。
原始证据位于 `.runtime/codex_plugin_core_status.json`，更新前配置备份位于
`.runtime/plugin_profile_repair/`。独立进程验证不代表当前聊天已重新连接：本轮当前聊天仍为 90 项，
下次连接后应在该聊天再次核验 20 项并调用 health。

后续验收（用户再次重启后）：当前聊天原生工具清单已实际切换为 core 的20项。
同次 health 返回 bridge_unavailable/Windows10061，表明9876桥接当时未监听；
不能将此连接状态误判为插件配置未更新。此次仅整理文档，未启动Blender或编辑工程。
经验总结位于 `D:\AI\MCP\Blender\经验文档\BlenderMCP插件配置缓存与重启排障经验.md`。

历史 core 曾只提供基础能力；当前 core 只决定初始文档视图，建模、Outliner、形态键和驱动器等全部常驻，无需换profile。
工具范围见 [TOOL_PROFILES.md](TOOL_PROFILES.md)。

## 为什么补做安装

先前仅通过 `codex mcp add blender` 写入独立 MCP 服务配置，并用官方 Python SDK 验证服务连通。这并不等于安装了可在插件页显示的插件，也不能证明已打开的聊天获得原生工具。用户重启后指出当前工具仍缺失，本轮补做实际插件安装。

## 文件与安装

源文件全部位于 `MCP/codex-marketplace/`：

- `.agents/plugins/marketplace.json`：本地目录 `blender-local`。
- `plugins/blender-mcp/.codex-plugin/plugin.json`：插件标识和显示名称 **Blender MCP**。
- `plugins/blender-mcp/.mcp.json`：启动本项目 Python / server.py，端口 9876，启动等待 60 秒、调用等待 120 秒。

在本机执行：

```powershell
codex plugin marketplace add D:\AI\MCP\Blender\MCP\codex-marketplace --json
codex plugin add blender-mcp@blender-local --json
```

Codex 返回安装路径 `C:\Users\12596\.codex\plugins\cache\blender-local\blender-mcp\0.1.0`，并在其用户配置写入启用标志。这是 Codex 管理的应用缓存；项目代码、Git、文档和日志仍在 `MCP`，未在工作区根目录创建 MCP 文件。

确认插件安装后，备份用户配置到忽略的 `MCP/.runtime/`，执行 `codex mcp remove blender` 去除旧的同名独立登记。再次 `codex mcp get blender --json` 仍能找到服务，工作目录和 60/120 秒超时来自插件清单，表明插件配置生效。

## 2026-10-03 实际验证

通过本机 Codex 0.160.0 的公开 app-server JSON-RPC 接口进行验证，没有创建新聊天或调用模型：

1. `plugin/installed` 返回 `blender-mcp@blender-local`：`installed=true`、`enabled=true`、`availability=AVAILABLE`、显示名称 `Blender MCP`。
2. `mcpServerStatus/list` 限定 `serverName=blender`：`pluginId=blender-mcp@blender-local`、服务已初始化、`toolsError=null`、42 个工具，含 `blender_health`。
3. 返回的 `runtimeStatus` 为 null，不能据此声称当前桌面会话已连接；工具清单和 serverInfo 则证明此次 Codex 验证进程成功初始化插件服务。

原始响应保存在 `.runtime/codex_plugin_installed.json`、`.runtime/codex_plugin_status.json`，验证脚本与服务输出同样在 `.runtime/`，不入 Git。

已打开的桌面应用需要刷新插件页或重启。接着必须在目标聊天验证原生 `blender_health` 调用，才将该聊天标记为接入成功；独立 SDK、另一个 app-server 进程和 CLI 配置查询都不能替代这一项。

官方接入说明：[MCP 配置](https://developers.openai.com/codex/mcp)；[本地插件目录与安装](https://developers.openai.com/plugins/build/plugins)。

## 2026-10-04 当前聊天复核

- 再次运行 `codex plugin list --json`，其 `installed` 列表包含 `blender-mcp@blender-local`，`installed=true`、`enabled=true`。此次记录保存在 `.runtime/codex_plugin_recheck_20261004.json`。
- 当前聊天直接调用原生 `mcp__blender__blender_health` 成功，返回 Blender 5.2.1 LTS、GUI PID 94804、`127.0.0.1:9876`。这次验证的是本聊天的原生工具，不是独立 SDK。
- 当前聊天可用 42 个 Blender MCP 工具；源码新增的 5 个场景工具需要刷新/重启 Codex 后重新加载工具清单。
- 用户截图只显示 Dsh；复核确认当前安装记录和聊天连接均已存在，但未通过电脑操作检查或刷新桌面插件页。页面仍不显示时，退出并重新打开 Codex，再检查显示名称 **Blender MCP**。保留当前 Blender 进程即可。
