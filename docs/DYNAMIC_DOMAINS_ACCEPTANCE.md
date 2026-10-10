> 历史记录：下文描述的“按域过滤 tools/list 与拒绝未选域调用”已被用户明确纠正。
> 当前语义为所有93项工具常驻，域仅用于查看文档。现行验收见 [TOOL_DIRECTORY_ACCEPTANCE.md](TOOL_DIRECTORY_ACCEPTANCE.md)。旧报告保留用于追溯，不作为当前通过证据。

# 动态全局/局部工具域验收

日期：2026-10-05。目标来自用户附件 `85630734-d6f1-40cc-8fd8-3dc0f209ef37/pasted-text-1.txt`。
本报告区分服务端实现、正式协议、真实客户端和已有业务保护，不将SDK模拟等同于Codex桌面已刷新。

## 现状与实现

| 项目 | 改造前 | 当前源码 |
| --- | --- | --- |
| 注册实现 | profile用remove_tool物理删除 | 完整注册表保留93项，正式handler过滤list/call |
| 默认 | 未设置变量all90 | global-only25，局部全关 |
| 控制 | 无 | 3项常驻目录/状态/切换工具 |
| 通知 | listChanged=false | 初始化tools.listChanged=true；真实会话发送list_changed |
| 分类 | 静态操作域与部分任务子集 | global25 + 10互斥local域68项；所有配置严格查漏 |
| 会话 | stdio进程 | 一个进程一个客户端会话，独立域状态 |

改动集中在 `blender_mcp/tool_profiles.py`、新增 `blender_mcp/dynamic_domains.py`、
`tools/__init__.py`和`tools/domains.py`。其它域业务逻辑与Blender实现没有重写。
原90项完整目录在改造前保存至 `tests/fixtures/legacy90_tools.json`，兼容检查比较名称、说明、
input/output schema及元数据。只有3项控制工具新增。

## 逐项证据

| 用户验收项 | 当前证据 |
| --- | --- |
| 1 默认global-only及global常驻 | 默认生产stdio清单25项；每个局部选择核对globals；所有global安全调用均不被域授权拒绝 |
| 2 任一域完整集合与schema | 单元及生产stdio逐个检查10域，和原90项完整定义比对 |
| 3 组合/追加/移除/清空/幂等 | 真实控制调用与重列清单；幂等不重复通知，不增加revision |
| 4 禁用工具不可调用 | 正式协议直接调用所有非选中工具，返回结构化tool_domain_disabled；录制bridge证明无调用副作用 |
| 5 输入/映射/失败安全 | 未知/重复/global域、数组上限、未分类注册、中途清单失败、通知失败/取消均拒绝或恢复原状态 |
| 6 能力/通知/客户端刷新 | mcp1.30.0正式ClientSession收到实际通知并重列更新缓存；真实Codex聊天25项启动及域控制验收完成，跨回合仍未应用新增工具，明确记录限制及精确启动域重连后备 |
| 7 会话/并发/离线 | 两生产stdio进程并行选不同域，授权不串扰；并发enable精确并集；bridge端口1不可用时仍查询/切换 |
| 8 完整分类/all/兼容 | 93项分区互斥无遗漏，all选10个local域；旧76/61项协议schema基线和原90项完整快照比较通过 |
| 9 生命周期保护 | 现有业务保护回归、launcher非法参数、open确认/schema和save越界负例；未启动/关闭Blender或替换用户工程 |

## 测试结果与命令

以下是已完成阶段的结果；新增断言或代码变化后应更新最终运行证据，不能将旧结果冒充最终结果。

- `.venv/Scripts/python.exe -m unittest tests.test_dynamic_domains tests.test_dynamic_domains_stdio`：最终30项通过，144.913秒；单元23项、正式生产stdio协议7项。新增实际builder未分类/缺失注册检查及初始化前公开SDK注册表检查。记录为 `.runtime/dynamic_domains/domain_tests_final.log`。
- `python -m unittest tests.test_unit_validation tests.test_audit_features tests.test_scene_isolation tests.test_connection_startup tests.test_review_maintenance`：130项通过。
- `.venv/Scripts/python.exe -m unittest tests.test_outliner tests.test_outliner_management tests.test_file_bootstrap tests.test_file_open tests.test_connection_startup tests.test_node_frames tests.test_tool_profiles`：最终246项通过，8.241秒。
- `python -m unittest tests.test_properties tests.test_properties_modes tests.test_geometry_nodes_edit tests.test_geometry_nodes_edit_safety tests.test_geometry_nodes_capture tests.test_geometry_nodes_capture_safety tests.test_model_inspection tests.test_review_maintenance tests.test_interface_defaults`：197项通过。
- `python tests/test_algorithm_review_profiles.py`：实际all93/review51/core25/outliner40/GN47/algorithm62，原76项input/output schema保持一致。
- `python tests/test_outliner_profiles_integration.py`：实际all93/core25/outliner40/GN47/algorithm62，原61项input/output schema保持一致，未知profile生产退出2且stdout无污染。

记录位于 `.runtime/dynamic_domains/`及原协议报告目录。正式通知证据
`.runtime/dynamic_domains/protocol/notification_report.json`：5次列表变化通知、SDK缓存重列更新、
同stdio PID、bridge离线。已有审计/传输测试的ResourceWarning仍存在；异步debug慢任务提示不是断言失败。
最初协议测试的5项清理错误来自unittest不同任务进入/退出AnyIO作用域；已将整个会话放在同一测试任务，
完整25项重跑通过。最初旧清单测试因新默认失败，改为显式all验证完整业务面，并由新测试覆盖默认global-only。

## Codex客户端：已知证据与边界

最终原生验收：用户再次重启后，本聊天实际提供global-only25项及三个控制工具。
直接调用域目录/状态均成功；原生replace Properties返回精确global+properties共30项、revision1和notification_sent=true。
同一回合两个调用边界及下一目标回合模型目录均仍25项；下一回合服务端仍保留revision1/Properties30，确认本次客户端未将新增工具应用到模型目录，不能宣称热更新成功，也不泛化到全部Codex版本。
原生三域组合51、enable Frame58、disable Frame51、重复replace不增revision、选择global结构化拒绝均完成；已clear并再次核对状态，恢复空局部域25项，当前模型目录也25项一致。无Blender业务调用及工程修改。
证据为 `.runtime/dynamic_domains/native_codex_refresh/report.json`，不是独立app-server或SDK模拟。
以下active-writer与旧26项为此前阶段，不能覆盖本次新启动及客户端限制事实。

1. 官方公开app-server文档和本机生成schema确认：`mcpServerStatus/list`可按threadId复用已有连接，
   `mcpServer/tool/call`需要已加载的threadId。
2. 不带threadId的独立状态查询建立临时测试MCP连接，返回25项及listChanged=true；查询后测试MCP进程已退出，
   所以不能据此证明运行期通知刷新。
3. 为取得持续连接，对当前聊天及同项目已有空闲聊天只读resume均返回`already has an active writer`。
   未绕过写入锁、未创建聊天/子代理/模型回合、未修改工程。此门禁不是“Codex不支持通知”的证据。
4. 早先桌面聊天为core20；经验文档整理时实际原生清单为旧algorithm_review26项，无新增域控制工具。源配置和安装缓存已为global-only且SHA256一致；独立Codex读取已安装插件返回25项、三个控制工具及listChanged=true。继续验收时只读查到的全部正式server.py进程均于20:24创建，早于20:50的动态实现；这些运行进程并未装入改造。目标聊天的具体PID尚未逐一关联，不能将新连接结果替代目标聊天验收。记录为 `.runtime/dynamic_domains/process_startup_evidence.json`。
   **目前不能宣称桌面模型已接收并应用动态变化。**

可审查的客户端探针为 `tests/verify_codex_domain_notifications.py`及测试专用fixture。
只采用进程级测试配置，不写用户插件配置；已有聊天必须无写入者，并核对rollout字节与mtime不变。
可成功运行时它通过真实Codex调用域控制并观察重列/缓存；当前门禁失败如实保存报告，不伪装绿色验收。

如果当前客户端未重列工具，明确的后备流程为：记录精确局部域 → 写入启动JSON数组
`BLENDER_MCP_LOCAL_DOMAINS` → 同步已安装插件 → 重新连接MCP → 在目标聊天核对清单/状态。
无需Blender重启、bootstrap、工程保存或模型重建。未持久化的运行期域选择会在重连时按初始配置重建。
这条后备的任意组合初始清单另由正式生产stdio测试核验。

## 生命周期与权限限制

本次没有提供任意代码接口、完整Blender重启或杀进程工具。离线bridge不能远程执行bootstrap；
启动工具仅在其原有无进程/无监听条件满足时建立新Blender；已有在线bridge可刷新5个固定模块。
open的显式工程替换确认、Project边界、关闭脚本和后验状态检查不变，保存/导出仍默认拒绝覆盖。
局部域只控制工具可用范围，不替代用户授权。客户端自身的工具安全策略不能被服务端强制覆盖。

没有commit/push，没有subagent，没有用户工程的真实写入测试。安装同步、新连接及目标聊天global-only加载均已验证；旧快照 `.runtime/dynamic_domains/documentation_chat_snapshot.json` 为此前26项阶段。最终原生证据在 `native_codex_refresh/report.json`：域控制可用，客户端未应用热变化，已恢复默认25项。客户端限制按用户验收第6项的明确允许分支交付，不能把它写成支持即时刷新；唯一后备为精确启动域、同步同一插件并重连。Blender桥接此前未监听，不影响离线域控制验收，本次没有要求启动用户工程来测试。

## 完成核查

已逐项对照原附件范围核查实现、公开SDK接口、完整分类、初始迁移、正式协议、并发隔离、原90项定义、既有业务保护、原生客户端及交付文档。证据一致性复核为 `.runtime/dynamic_domains/final_completion_audit.json`；复核脚本只核对证据，不替代行为测试。最终没有待完成的服务端功能或文档；客户端未应用列表变化这一已实测限制，按用户第6项允许的明确限制和重连后备分支交付。当前局部域已清空且目标聊天25项一致，不留待恢复状态。git diff与任务文档链接检查通过；未创建子代理或commit/push。

## 参考资料

- [MCP工具能力与列表变化通知](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [Codex公开app-server接口](https://learn.chatgpt.com/docs/app-server)
- 锁定SDK本机公开源码：`.venv/Lib/site-packages/mcp/server/lowlevel/server.py`、
  `server/session.py`、`server/fastmcp/server.py`；准确版本与API已实际检查，未照抄其它版本。
