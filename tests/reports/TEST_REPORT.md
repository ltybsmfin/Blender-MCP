# Blender MCP 测试报告（本会话实测）

## 参数化椅子与 GUI 显示验收：2026-10-03

使用真实 GUI Blender 5.2.1 LTS、官方 MCP SDK stdio 传输执行全部椅子配方阶段，结果成功。
图编辑事务批次 21 / 64 / 100 / 33 步均提交成功；节点组 65 节点、144 链接、8 输入，Join 多输入链接最终 10 条。
座宽 .48→.60 后实际宽度增加 .12 m；座高 .45→.52 后实际最高点增加 .07 m，均复位成功。
9 项几何断言全部通过，默认包围盒尺寸 `[.48, .501818, .870148]` m。

`blender_present_object` 在实际 GUI 中返回选中、活动对象与 local-view 均正确，完整椅子的取景距离为约 1.361 m，视角持续保留。
MCP 内 Windows 前台请求如实回报未获允许；不把窗口激活请求等同于激活成功。后续依用户“不要置前”要求全部使用 `foreground=false`。
该显示工具的单元保护 3/3 通过，原单元套件 58/58 通过；这次未重复运行此前完整 371 项基线。

实际渲染返回 960×720 原生图片内容，已检查模型完整入镜。工程首次保存成功、文件 794,766 字节。
证据：`MCP/.runtime/chair_inspect.log`、`chair_seat.log`、`chair_legs.log`、`chair_back.log`、`chair_verify.log`、`chair_save.log`。
工程与图像分别位于 `Project/chairs/parametric_chair.blend`、`Project/previews/parametric_chair.png`。

## 最终完整验收：2026-10-03（Asia/Shanghai）

Codex 在本机正常账户环境完成全部测试，使用实际安装的 **Blender 5.2.1 LTS** 与官方 Python MCP SDK 的 stdio 客户端。
**8 套测试、371 项检查全部通过；运行器退出码 0。** 下方早期受限构建环境记录仅保留为历史证据。

| 套件 | 检查数 | 结果 |
| --- | ---: | --- |
| 单元与参数校验 | 58 | 全部通过 |
| 传输与启动回归 | 7 | 全部通过 |
| 审计、看板与工具 schema | 30 | 全部通过 |
| Blender 几何节点、形态键与驱动器集成 | 49 | 全部通过 |
| Blender 引用、恢复与轮询回归 | 41 | 全部通过 |
| Blender 事务、截图与审计 | 64 | 全部通过 |
| MCP stdio 基线联调 | 85 | 全部通过 |
| MCP stdio 新工具联调 | 37 | 全部通过 |
| 合计 | **371** | **全部通过** |

运行目录 `D:\AI\MCP\Blender\MCP`，命令：

```powershell
$env:BLENDER_MCP_FORCE_SDK_STDIO = '1'
& '.\tests\run_all_tests.ps1' -BlenderPath 'D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe' -Port 0
```

完整输出保存于 `MCP/.runtime/host_final_verification.log`（运行证据不入库）。
汇总器遇到 `localhost` / `LOCALHOST` 的 JSON 键大小写冲突时采用原文摘要；相应套件自身退出码为 0、41 项全部通过。

### 实际 GUI Blender 的 MCP 预览验收

在独立的 Blender 5.2.1 LTS GUI 测试进程中，经官方 SDK stdio 客户端调用 `blender_capture_preview`，无需桌面自动化：

- `mode=viewport`：`GN_Panel` 生成 960×720 PNG，并返回一项原生 MCP `ImageContent`。
- `mode=render`：`GN_Morph` 生成 960×720 PNG，并返回一项原生 MCP `ImageContent`。
- 两次调用均成功。截图前后对比选中集、活动对象、视口旋转/位置/距离/投影模式、场景帧与子帧、渲染设置、相机以及数据块数量，均无变化。
- PNG 位于 `Project/previews/host_viewport_geometry_nodes.png` 和 `Project/previews/host_render_geometry_nodes.png`，图片已检查，模型完整入镜。
- `blender_log_dashboard(action=status)` 报告独立只读看板运行中，日志目录为 `MCP/.runtime/logs`。

证据：`MCP/.runtime/host_gui_mcp_check.json`、`host_gui_mcp_check.log`、`host_gui_states.json`。
上述 GUI/API 验收补全了历史记录中“GUI 未验证”和“无法在实际 Project 落盘”的限制。
按用户最新要求，后续 Blender 操作与验收不使用 computer-use；预览图片由 Blender MCP 直接生成。

### 事务范围

默认事务仅覆盖被编辑的节点组，成功会替换该节点组数据块身份；不提供整个 .blend 的回滚。
提交故障测试覆盖原生引用重定向、fake-user 与修改器输入恢复，并验证失败后仍可再次正常提交。
完整测试前进一步将原生重定向模式标记提前至调用前，并在反向重定向后再次恢复修改器输入，以处理部分重定向失败。

---

## 历史：验收修复增量复核（2026-10-03，Asia/Shanghai）

> 本节及以下内容是最终完整验收之前的记录；当前结果以本文档开头为准。

基线 Git 提交 **`18c300e`**；工作区根目录 `README.md` / `.gitignore` 已按用户授权删除（历史副本在
`MCP/docs/history/`）。本轮按 Codex 集成评审修复待办，只跑受影响的三套测试，**不再追**已知的受限沙箱
基线错误（写 `MCP` 之外 / `tempfile` 清理）。

| 套件 | 检查数 | 结果 | 本轮新增检查 |
| --- | ---: | --- | --- |
| 特性单元：审计 / 看板 / schema（`test_audit_features.py`） | 30 | **全部通过** | +5：待处理计数、看板操作计数、看板 UI 状态保持、初始探测失败后的关联恢复、legacy 部分批次 `ok=false` |
| Blender 特性：事务 / 截图 / 审计（`blender_feature_integration.py`） | 64 | **全部通过** | +8：源帧渲染、22 单位立方体完整入镜与四边留白、竖幅取景、`current` 视图语义、当前场景限定、视口状态恢复、实例几何或 realize 提示、提交故障注入 |
| 新工具 stdio 联调（`test_mcp_stdio_features.py`） | 37 | **全部通过** | 0（改动触及 MCP 桥包装器，故重跑确认无回归） |

命令：

```powershell
.\.venv\Scripts\python.exe tests\test_audit_features.py
D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe --background --factory-startup --python-exit-code 1 --python tests\blender_feature_integration.py
.\.venv\Scripts\python.exe tests\test_mcp_stdio_features.py
```

### 逐项证据（对应评审意见）

| 评审意见 | 修复 | 检查 |
| --- | --- | --- |
| 会话结束后看板仍显示"进行中 4" | 事件配对计数：只有活动会话中未闭合的 start 才算 `running`，已结束会话的未闭合 start 记 `info` | `test_pending_counts_only_outstanding_work_in_live_sessions`、`test_viewer_reports_operation_counts_not_historical_event_counts` |
| 计数口径不清 | 四个数字明确为**操作**数，并显示读入/返回/匹配/info/已结束会话；`counts.note` 说明口径 | 同上 + `test_dashboard_page_is_cjk_and_dependency_free` |
| 刷新会折叠详情、跳动滚动 | 按行 key 复用 DOM，未变化的行不重写、`open` 状态保留、`follow` 未勾选不滚动 | `test_dashboard_preserves_rows_scroll_and_expansion` |
| 初始 health 探测失败后关联能力永久丢失 | 传输失败或 health 报告 `audit_protocol` 时重新武装；新增 `correlation` 字段与 `bridge_audit_regained` 事件；不重放请求 | `test_correlation_is_regained_after_an_initial_failure` |
| 视口截图改变选中集/活动对象 | 快照与恢复补齐选中集、活动对象、子帧、fps/fps_base | `viewport_state_restore_covers_selection_active_and_render_state` |
| 预览把其它场景的网格也算进来 | 只扫描当前视图层可见网格 | `preview_scopes_to_the_current_scene_only` |
| 临时渲染场景固定在 frame 1 | 复制源场景帧/子帧/fps/起止帧与场景自定义属性 | `preview_renders_the_source_frame_not_frame_one`（同一固定相机下 22 单位模型的像素宽度 > 3× 4 单位） |
| 相机按最大边长估导致 22 单位立方体被裁 | 相机空间投影拟合（`sensor_fit=HORIZONTAL` + 12% 留白 + 投影深度裁剪面），且相机矩阵由 location/rotation 构造 | `camera_fit_keeps_the_whole_22_unit_model_inside_with_a_margin`、`camera_fit_handles_a_portrait_frame` |
| `current` 视图被改写成 isometric | 去掉调用方的等轴测强制转换，走复用场景相机的分支 | `current_view_uses_the_source_camera_instead_of_isometric` |
| 纯实例几何被误报空几何 | 从 `depsgraph.object_instances` 取点算包围盒（有上限/截断标记）；否则给出 `realize_instances` 可执行提示 | `instance_only_geometry_is_measured_from_the_dependency_graph` / `..._or_gets_a_realize_hint` |
| 发布前先删旧图，失败会丢图 | 去掉预删除，直接原子 `os.replace`；失败时旧图保留并写明 | 代码路径 + `preview_writes_into_project_previews_only`、`existing_preview_requires_overwrite` |
| 提交失败后 `use_fake_user` 未恢复；输入还原失败/旧块删除失败仍报成功 | 原生 `ID.user_remap` + 统一撤销（反向 remap、恢复 fake_user、还原名称、删除草稿），四种失败一律报错 | `commit_aborts_when_a_modifier_input_cannot_be_restored`、`commit_aborts_when_the_old_datablock_cannot_be_removed`、`commit_still_works_after_a_fault_rollback` |
| `_batch_audit` 不得重复 duration_ms、legacy 部分失败须 `ok=false` | 去掉重复关键字，`ok = (status == "ok")` | `test_legacy_partial_batch_is_audited_with_a_truthful_ok_flag` |
| 结果映射 / 无头视口检测不得回归 | 未改动这两处，重跑确认 | `preview_tool_returns_native_content`、`auto_mode_falls_back_to_render_when_headless`、`preview_viewport_needs_gui`、`test_preview_returns_native_image_content_and_audits_without_base64` |

### 本轮改动文件

`blender/blender_mcp_addon.py`、`blender_mcp/audit.py`、`blender_mcp/log_viewer.py`、
`tests/test_audit_features.py`、`tests/blender_feature_integration.py`、
`README.md`、`docs/ARCHITECTURE.md`、`docs/DEVELOPMENT_LOG.md`、`tests/reports/TEST_REPORT.md`。
未改动：`bridge.py`、`config.py`、`models.py`、`tools/operation_log.py`、`tools/preview.py`、
`bootstrap.py`、`launch_blender.ps1`、`.venv`、沙箱权限、全局配置；无提交/推送。
未触碰：`.runtime/host_*`（Codex 所有）、`Project/previews` 中的 Codex 样本图、
用户自己的 Blender/Python 进程与已运行的看板进程。

### 局限

- GUI 视口截图路径仍需在有窗口的 Blender 中人工确认（Codex 负责）；本轮只覆盖无头下的可执行报错路径
  与视口状态快照/恢复的单测。
- 截图落盘仍受沙箱限制：真实 `Project\previews` 由单元测试（伪造桥 + 真实 PNG 字节 + 补丁后的
  `PROJECT_DIR`）与真实 Blender 测试（`BLENDER_MCP_WORKSPACE` 隔离工作区）分别间接验证；
  stdio 下断言的是清洁错误信息而非成功落盘（见下文第 2 节）。
- 首次渲染需初始化 GPU 管线（实测约 20 秒，之后同进程 <1 秒），默认超时 45 秒。

---

# 第一版测试报告（历史，保留证据）

## 最终复核：2026-10-03（Asia/Shanghai）

> 本节的套件计数是第一版（验收修复前）的记录，保留作为基线证据；
> 最新的增量复核见本文档开头的「验收修复增量复核」。

本轮在**受限构建沙箱**（DSH `workspace-write`：只允许写 `D:\AI\MCP\Blender\MCP` 内，且拒绝 `os.chmod`）
中运行。新增的审计 / 看板 / 事务批量编辑 / 截图预览四组功能全部通过；原有 230 项基线中，
**6 项因沙箱限制失败**（3 项 `tempfile` 清理、3 项写入同级 `Project\`），均为环境限制、与本次改动无关。

| 套件 | 检查数 | 结果 | 说明 |
| --- | ---: | --- | --- |
| 服务端单元与协议（`test_unit_validation.py`） | 58 | 55 通过 / 3 环境失败 | 新增 2 项：命令清单含 `capture_preview`、批量事务默认值 |
| 连接期限、配置、启动参数回归（`test_review_transport.py`） | 7 | 全部通过 | 未改动（该套件只打印，无 JSON 报告） |
| 特性单元：审计 / 看板 / schema（`test_audit_features.py`） | 25 | **全部通过** | 新增 |
| 原始 Blender 集成（`blender_integration.py`） | 49 | 42 通过 / 1 环境失败 / 6 未执行 | 新增 1 项事务提交断言；在「保存并重开演示 .blend」处被沙箱中断 |
| 数据块引用、恢复和轮询回归（`blender_review_regressions.py` → `review_report.json`） | 41 | 全部通过 | 未改动（批量默认事务后仍全绿） |
| Blender 特性：事务 / 截图 / 审计（`blender_feature_integration.py`） | 54 | **全部通过** | 新增 |
| 官方 SDK stdio 端到端（`test_mcp_stdio.py`） | 85 | 83 通过 / 2 环境失败 | 新增 7 项：新工具、事务断言、回滚负例 |
| 新工具 stdio 联调（`test_mcp_stdio_features.py`） | 37 | **全部通过** | 新增 |

记录在案的检查合计 **350 项**（58 + 7 + 25 + 43 + 41 + 54 + 85 + 37），
失败 **6 项**全部为沙箱环境限制（见第 2 节），功能代码相关失败为 **0**。

执行命令（在 `MCP` 目录）：

```powershell
.\tests\run_all_tests.ps1 -BlenderPath 'D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe' -Port 0
```

单项复跑：

```powershell
.\.venv\Scripts\python.exe tests\test_unit_validation.py
.\.venv\Scripts\python.exe tests\test_audit_features.py
D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe --background --factory-startup --python-exit-code 1 --python tests\blender_integration.py
D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe --background --factory-startup --python-exit-code 1 --python tests\blender_feature_integration.py
.\.venv\Scripts\python.exe tests\test_mcp_stdio.py
.\.venv\Scripts\python.exe tests\test_mcp_stdio_features.py
```

JSON 报告位于 `tests\reports\`：`unit_report.json`、`review_report.json`、`feature_unit_report.json`、
`blender_integration_report.json`、`blender_feature_report.json`、`stdio_report.json`、`stdio_feature_report.json`。

---

## 1. 新增功能与对应证据

### 1.1 审计日志（每一次操作都有记录）

| 要求 | 证据（检查名） |
| --- | --- |
| 参数、结果、错误、耗时全部落盘 | `test_audit_features.py: test_tool_success_is_audited_with_args_result_and_duration`；`blender_feature_integration.py: audit_success_event_has_timestamp_duration_and_result` |
| UTC 时间戳 + session / request 关联 + 递增 seq | 同上（断言 `ts` 以 `Z` 结尾、`session`/`pid`/`seq` 存在） |
| MCP 侧与 Blender 侧用同一请求号关联 | `test_bridge_call_is_audited_and_correlated`、`fdriver…`（`blender_feature` 里的 `audit_records_every_bridge_command`） |
| SDK 参数校验失败也要记录 | `test_schema_validation_failure_is_audited`、`audit_records_schema_validation_failures_at_dispatch` |
| 批量编辑逐步 + 事务结论 | `test_batch_steps_and_rollback_are_audited`（3 条 `batch_step` + 1 条 `batch_rollback` + `tool_end.status=rolled_back`） |
| 大参数有界 + 哈希 | `test_large_values_are_bounded_and_hashed`（5000 项数组 → `length` + `sha256` + 20 项样本） |
| 绝不出现 token | `test_secret_keys_and_token_text_never_written`、`audit_never_contains_the_bridge_token`（含 64 位十六进制正则兜底） |
| 轮转 + 归档 + 读取不受影响 | `test_rotation_keeps_archives_and_reading_survives`、`audit_rotation_keeps_bounded_archives_and_keeps_logging` |
| 多进程不共享文件 | `test_two_processes_never_share_a_file` |
| 日志写失败不致命、只报 stderr | `test_write_failure_is_reported_but_never_raised` |

### 1.2 实时日志看板

| 要求 | 证据 |
| --- | --- |
| 本地回环、只读、有界 | `test_events_endpoint_is_bounded_and_filtered`（`limit` 上限 500）、`test_read_only_and_bounded_surface`（POST→405、未知路径→404、路径型参数不当作文件） |
| 状态筛选 / 自动滚动 / 详情 / 中文 | `test_dashboard_page_is_cjk_and_dependency_free`（含 成功/错误/进行中/已回滚/自动滚动/暂停刷新；断言无 CDN） |
| 不显示图片 base64 | `test_viewer_never_serves_image_base64`、`operation_log_is_secret_and_base64_free` |
| 启动方式：MCP 工具 / PS 脚本 | `test_dashboard_and_log_tools_work_through_the_tool_layer`、`dashboard_starts/serves/stops`（stdio 实测 URL 可访问） |
| 端口冲突明确报错、元数据原子发布 | `test_port_conflict_is_explicit`、`test_health_and_metadata_and_reuse` |

`MCP\launch_log_viewer.ps1` 人工实测（本沙箱）：

```text
> powershell -ExecutionPolicy Bypass -File MCP\launch_log_viewer.ps1 -Seconds 30
LOG_VIEWER_URL http://127.0.0.1:22112/
[log-viewer] pid 53116 | root D:\AI\MCP\Blender\MCP\.runtime\logs | 只读本地看板，仅回环监听

> powershell -ExecutionPolicy Bypass -File MCP\launch_log_viewer.ps1        # 复用已运行的看板
LOG_VIEWER_URL http://127.0.0.1:35714/
[log-viewer] reusing pid 38612 (root D:\AI\MCP\Blender\MCP\.runtime\logs)

> powershell -ExecutionPolicy Bypass -File MCP\launch_log_viewer.ps1 -Stop
no log viewer started by this script is running        # 陈旧元数据被忽略，不误杀无关进程
```

说明：沙箱拒绝 `Get-CimInstance`（进程命令行核对），脚本已按设计退化为「健康探测 + PID 元数据」双重确认后再停止；
清理时只停止本会话启动的看板进程，未触碰用户自己的 Blender / Python 进程。

### 1.3 事务化批量编辑

| 要求 | 证据（`blender_feature_integration.py`，真实 Blender） |
| --- | --- |
| 失败整体回滚，原节点组逐字段一致 | `rollback_reports_rolled_back_truthfully`、`rollback_leaves_the_original_group_byte_for_byte`（节点/链接/默认值/接口逐项比较）、`rollback_keeps_the_datablock_identity`（指针相同） |
| 失败不留草稿/备份数据块 | `rollback_leaves_no_staging_or_backup_group` |
| 修改器参数、驱动器目标、求值几何不变 | `rollback_keeps_modifier_inputs_and_driver_targets` |
| 兼容性选项在改动前被拒绝 | `incompatible_options_are_rejected_before_any_mutation` |
| 成功提交：身份替换 + 使用者重定向 | `commit_reports_committed_truthfully`、`commit_replaced_the_datablock_identity_under_the_same_name`、`commit_remapped_every_enumerated_user`（修改器 1 / 嵌套节点 1 / 驱动器目标 1） |
| 修改器输入值保留且仍接在新图上 | `commit_repointed_the_modifier_and_kept_its_input_values`、`modifier_input_is_still_wired_to_the_new_graph`（Rows 5 → 20 顶点） |
| 外部驱动器与嵌套组引用保留并求值 | `commit_kept_the_driver_target_and_its_evaluated_value`、`driver_still_evaluates_against_the_new_datablock`、`commit_repointed_the_nested_group_reference` |
| 节点组**自身动画**（树上的驱动器）保留 | `fixture_group_has_its_own_driver`（建组前先挂 `["phase_out"]` 驱动器并求值为 3.0）、`commit_preserved_the_group_own_animation`（替换数据块后驱动器、表达式与求值都不变） |
| 旧模式（非事务）保持可用 | `legacy_batch_mode_is_not_transactional`、`legacy_partial_mode_keeps_going_after_a_failure`、`legacy_partial_mode_leaves_partial_edits_applied` |
| 链接库节点组拒绝 | `linked_library_group_is_refused_transactionally`（真实 `.blend` 链接库 + `unsafe_linked_group`） |

stdio 侧同口径验证：`transactional_batch_commits`、`failing_transaction_is_an_error`（错误文本含 `graph_edit_rolled_back`）、
`failed_transaction_left_the_group_unchanged`、`incompatible_options_are_rejected`、`legacy_mode_reports_partial_and_keeps_edits`。

### 1.4 截图预览

| 要求 | 证据 |
| --- | --- |
| 返回 **MCP image 内容**而不只是路径 | `test_preview_returns_native_image_content_and_audits_without_base64`（进程内，真实 PNG 字节 → `ImageContent`，元数据 `mcp_image_content=true`）；stdio 侧另有 `preview_tool_returns_native_content`（无 output schema） |
| 真实渲染且尺寸正确、非空白 | `preview_dimensions_and_size_match_the_request`（320×240，65964 字节）、`preview_png_is_a_real_non_blank_image`（重新载入像素：min 0.251 / max 0.565 / 76 个不同取值） |
| 按**求值后**几何取景 | `preview_frames_the_evaluated_geometry_not_the_basis_mesh`（16 顶点 = 网格节点输出，基础网格只有 4 个顶点；包围盒 z=2.0 反映提交后的 Shift 节点） |
| 场景设置可还原、不留临时数据块 | `preview_restores_the_whole_scene_state`（分辨率/引擎/相机/帧/数据块列表完全一致）、`preview_leaves_no_temporary_datablock_behind` |
| 模式语义 | `auto_mode_falls_back_to_render_when_headless`（回报实际 `render`）、`preview_viewport_needs_gui`（`viewport_unavailable`） |
| 路径与尺寸限制 | `preview_path_rules_are_enforced_inside_blender`、`preview_dimensions_are_bounded`、`existing_preview_requires_overwrite`、`empty_geometry_is_reported_with_diagnostics` |
| 文件只写 `Project\previews` | `preview_writes_into_project_previews_only` |

---

## 2. 环境限制（诚实说明，未使用任何 shim）

沙箱允许写 `D:\AI\MCP\Blender\MCP` 内的文件，并**拒绝 `os.chmod`**。由此产生 6 项与代码无关的失败：

| 失败项 | 直接原因（实测） |
| --- | --- |
| `test_rejects_symlinked_component_when_available` | 需要在同级 `Project\` 建目录做符号链接夹具：`PermissionError: [WinError 5] 'D:\AI\MCP\Blender\Project\real_dir'` |
| `test_token_is_created_stable_and_not_leaked_in_health` | `tempfile.TemporaryDirectory()` 清理时 `os.chmod`：`PermissionError: WinError 5`（在 `.runtime\tmp` 与系统临时目录下同样发生） |
| `test_config_ensure_token_matches_addon_file` | 同上 |
| `blender_integration.py: integration_run_completed` | 保存演示文件到同级 Project：`Permission denied: 'D:\AI\MCP\Blender\Project\demo\parametric_demo.blend'`（前 43 项检查全部通过） |
| `test_mcp_stdio.py: save_blend_first_time` | 同上（`save_as_mainfile` 无法写 `Project\demo`） |
| `test_mcp_stdio.py: save_blend_writes_inside_project` | 同上 |

最小复现（与本次改动无关的纯环境行为）：

```powershell
# 1) 平台临时目录不可写文件 / 不可 chmod
.\.venv\Scripts\python.exe -c "import tempfile,os; d=tempfile.mkdtemp(); open(os.path.join(d,'f'),'w')"
#   → PermissionError: [Errno 13] ...\dsh-XXXX\tmpXXXX\f   （文件沙箱拒绝）

# 2) 同级 Project 目录不可写
D:\AI\MCP\Blender\Version\5_2_1_LTS\blender.exe --background --factory-startup `
  --python-expr "import pathlib; pathlib.Path(r'D:\AI\MCP\Blender\Project\.probe').write_text('x')"
#   → PermissionError: [Errno 13] 'D:\AI\MCP\Blender\Project\.probe'
```

因此：

- 截图预览的**完整落盘路径**在真实 `Project\previews` 下无法在本沙箱验证；改用 add-on 自带的
  `BLENDER_MCP_WORKSPACE` 覆盖（`MCP\.runtime\feature_workspace`）跑通全流程 —— 路径规则本身未放宽：
  仍然必须是 `<workspace>/Project/previews/*.png`，测试另有一项
  `default_project_directory_is_the_sibling_project_folder` 断言默认工作区就是同级 `Project`。
- `blender_capture_preview` 经官方 stdio 调用时，文件写入同样被沙箱拒绝；测试断言它返回
  **可执行的清洁错误**（`preview_dir_unwritable` / `preview_write_failed`，并指名 `Project\previews`），
  而不是崩溃或静默失败。

未使用的规避手段（保持诚实）：没有改沙箱、没有改 venv/SDK、没有临时 `sitecustomize.py` shim、
没有跳过（skip）任何断言、没有把失败的检查从清单中删除。

另外记录两处沙箱遗留（与功能无关，供人工清理）：

- 仓库根目录下残留两个空目录 `MCP\tmpe4yyse6d\`、`MCP\tmpj6avimxw\`（0o700）。
  来源：在不设置 `TMP`/`TEMP` 的情况下手工运行单元测试时，Python `tempfile` 回退到当前目录创建了它们，
  随后 `os.chmod`/删除被沙箱拒绝。`tests\run_all_tests.ps1` 会把 `TMP`/`TEMP` 指向 `MCP\.runtime\tmp`，
  因此正常走 runner 不会在仓库根目录产生这类目录。它们为空目录、不在 `.gitignore` 中，
  但 git 因权限拒绝读取而未列出；可在普通账户下直接删除。

---

## 3. Blender 5.2.1 实测行为（本版新发现，已写入实现）

1. **`tree.copy()` 会复制节点树的动画数据与驱动器**（探针实测 `copy.animation_data.drivers == 1`），
   因此事务替换数据块后树自身的驱动器仍在；实现里仍保留 `_sync_tree_animation` 校验/兜底。
2. **把 `mod.node_group` 指向新数据块会保留修改器的逐实例输入值**（按 socket identifier 存储）；
   实现仍显式快照并回写，并在响应里报告 `modifier_inputs.restored`。
3. **替换数据块后必须给引用方打 tag**：`_flush_remapped` 对每个被重定向的属主调 `update_tag()`，
   否则修改器/驱动器可能沿用旧数据块；
4. **只改自定义属性不触发驱动器重算**：需要 `ID.update_tag()`（或 `set_custom_property` 这类打 tag 的路径）
   + 依赖图更新，`frame_set(当前帧)` 不会重新求值（因此实现没有用「跳帧」这种副作用手段）。
5. **`--background` 下没有可用 3D 视口**：`bpy.ops.render.opengl` 直接报
   `Cannot use OpenGL render in background mode (no opengl context)`；
   `window_manager.windows` 在 5.x 无头下非空，所以视口可用性必须用 `bpy.app.background` + 区域尺寸共同判断。
6. **引擎枚举是动态的**：`RenderSettings.bl_rna.properties["engine"].enum_items` 只列出当前值，
   可用引擎必须靠「尝试设置」来判定；本机可用 `BLENDER_WORKBENCH` / `BLENDER_EEVEE` / `CYCLES`，
   没有 `BLENDER_EEVEE_NEXT`。首个渲染含 GPU 管线初始化（实测约 20 秒），之后同进程 <1 秒。
7. **无用户的节点组不会被写进 .blend**：做链接库测试前必须给源节点组 `use_fake_user = True`，
   否则 `libraries.load` 的 `source.node_groups` 为空。
8. **同名数据块可跨库共存**：链接进来的节点组可能与本地节点组同名，按名称查找会产生歧义；
   测试通过先重命名本地组来固定链接组名称。

---

## 4. 与基线的差异汇总

- 工具数：38 → **41**（新增 `blender_capture_preview`、`blender_operation_log`、`blender_log_dashboard`）。
- 桥命令数：+1（`capture_preview`），`batch_graph_edit` 新增 `transactional` 参数（默认 `true`）。
- 检查数：230 → **350**（新增 3 套共 116 项功能检查，另有 10 项断言加入既有套件：
  单元 +2、Blender 集成 +1、stdio +7；集成套件因沙箱中断少执行 6 项）。
- 未改动：驱动器与形态键行为、路径校验与安全规则、启动引导（`bootstrap.py` / `launch_blender.ps1`）、
  桥接传输与鉴权（仅在请求参数里增加一个由插件在校验前摘除的保留字段）。
