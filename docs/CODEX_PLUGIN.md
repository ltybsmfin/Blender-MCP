# Codex 本地插件接入

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
