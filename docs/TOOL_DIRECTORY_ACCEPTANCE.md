# 按界面查看工具目录：验收记录

2026-10-05。用户要求模型可以选择查看某个界面的工具列表，所有工具常驻生效。先前按域过滤可用工具的实现与此要求不同，本次移除了过滤与调用拦截。

## 当前行为

- 实际 server.py 的 tools/list 在任何 profile、目录状态下均为93项。
- blender_tool_domains 可按11个分类查询真实工具名称、说明和 input/output schemas；省略 domains 返回分类概览。查询不改目录状态。
- blender_set_tool_domains 的 replace/add/remove 只改文档阅读视图，enable/disable 兼容 add/remove，不改变调用权限。
- 浏览 Outliner、Properties 或清空阅读目录后，其他分类工具仍直接进入原业务处理。
- tools.listChanged=false，目录操作无列表变化通知，无需重列或重连。
- 原90项完整定义与改造前 tests/fixtures/legacy90_tools.json 一致，未重生成基线。
- 分类完整性启动检查、独立会话目录状态及原审计保留；目录锁不阻塞业务调用。
- 文件替换确认、路径边界、覆盖保护与原参数校验保留。本次未启动或编辑 Blender。

## 验证结果

| 检查 | 结果 | 本地证据 |
| --- | --- | --- |
| 目录单元与实际stdio协议 | 23项通过 | .runtime/domain_browsing/tests.log、protocol/report.json |
| 审计、参数与既有功能 | 88项通过 | .runtime/domain_browsing/audit_regression.log |
| Outliner、文件、Frame、连接、目录兼容 | 239项通过 | .runtime/domain_browsing/related_regression.log |
| 六种参数化检查profile | 每种93项，旧76项schema一致 | .runtime/algorithm_review_profiles/report.json |
| 五种Outliner profile | 每种93项，旧61项schema一致 | .runtime/outliner_profiles_integration/report.json |
| 独立真实Codex读取已安装插件 | 同一插件93项，listChanged=false | .runtime/codex_tool_directory_status.json |

239项包含16项目录单元，重复执行不累计为独立新增测试。
首次混合 fake bpy 与“server不导入bpy”测试时，311项中3个环境隔离断言失败；
保留 regression.log，未放宽断言，拆分进程后88项与239项均通过。
既有测试退出时审计文件句柄的 ResourceWarning 保留记录，本次未修复。

执行入口：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_dynamic_domains tests.test_dynamic_domains_stdio
.\.venv\Scripts\python.exe -m unittest tests.test_unit_validation tests.test_audit_features
.\.venv\Scripts\python.exe -m unittest tests.test_outliner tests.test_outliner_management tests.test_file_bootstrap tests.test_file_open tests.test_node_frames tests.test_connection_startup tests.test_tool_profiles
.\.venv\Scripts\python.exe tests\test_algorithm_review_profiles.py
.\.venv\Scripts\python.exe tests\test_outliner_profiles_integration.py
```

## 当前聊天的加载边界

通过官方 Codex app-server 的 plugin/installed 与 mcpServerStatus/list，独立新连接确认已安装的
blender-mcp@blender-local 实际取得93项；未创建聊天、运行模型或修改用户配置。

当前聊天在修改前加载的原生目录仍25项，工具描述仍是旧启用/禁用域语义。
独立连接成功不能代表当前聊天的旧进程已经替换。旧 MCP 进程需要一次重连装入修改后的程序；
若没有重连入口，可完整退出再打开 Codex。无需重启 Blender 或运行 bootstrap。
加载后所有工具常驻，按界面浏览不再要求重连。

旧 DYNAMIC_DOMAINS_ACCEPTANCE.md、.runtime/dynamic_domains/ 和 codex_dynamic_plugin_status.json
保留用于追溯，不代表当前约定。现行用法见 [TOOL_PROFILES.md](TOOL_PROFILES.md)。
