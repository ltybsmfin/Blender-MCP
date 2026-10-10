# 原生 Frame 与独立节点注释扩展

## 调查与能力矩阵

2026-10-04 实际读取了 MCP README、tools 注册、geometry_nodes.py、bridge command/错误/审计实现、bootstrap，以及本机 Version/5_2_1_LTS 下的扩展加载代码。MCP/AGENTS.md 不存在；遵循用户提供的 AGENTS 分工。可执行程序 `blender.exe --version` 返回 **Blender 5.2.1 LTS**，不是根据目录名推断。

| 能力 | 修改前 | 实现/复用 |
|---|---|---|
| 创建 Frame | 通用 create_node(NodeFrame) 已可用 | 保留旧工具；专用 create 增加精确名称、幂等、类型/归属校验 |
| 查节点 parent/label/location | inspect_node_group 已支持 | 保留旧返回；get/layout 补充画布位置、子节点、Frame 属性、Text |
| 设置标签/颜色/shrink | 通用 RNA 属性部分支持 | 保留旧工具；update 提供有界原子校验 |
| 归组/解除归组 | 无可调用的专用 MCP 工具；原生 parent 属性不等于 MCP 支持 | parent/unparent 检查全部名称/循环，保留画布坐标、尺寸和计算连接 |
| Frame.text/Text 写入 | Text 指针不在通用引用类型中；缺少稳定正文工具 | set_text：稳定内部 Text、共享检查、明确覆盖/清空政策 |
| 独立注释交互/探测 | 缺失 | Mod Extension + Blender 端适配器；不改变通用建模工具的依赖 |

节点树名称仅精确定位 `bpy.data.node_groups` 中的 Geometry/Shader/Compositor/Texture NodeTree 数据块。嵌入 Material/World/Light/Scene 的树仅用于检查 Text 是否共享，不作为目标树进行跨树查找。已有通用工具的 schema、默认行为及返回结构未改。

## 原生工具（插件缺失也能用）

所有工具以 `blender_node_frame_` 开头，必需参数 `group` 是节点组数据块名称。

| 后缀 | 其它参数 | 行为 |
|---|---|---|
| create | frame, label='', location=(0,0), color=null, label_size=24, shrink=false | 创建原生 Frame；已有同名 Frame 原样返回，非 Frame 拒绝 |
| get | frame | 原生属性、父子、画布位置、Text 名称/正文/用户/引用 |
| parent | frame, nodes, create_frame=false | 默认不自动创建；全部目标预校验，保持画布世界位置，循环拒绝 |
| unparent | frame, nodes | 只解除指定 Frame 的直接子节点；其它归属跳过；位置不变 |
| update | frame, label/color/label_size/shrink/width/height=null | 未传属性保持；label_size 8..64，尺寸 100..10000；写失败恢复 |
| set_text | frame, body, overwrite=false, allow_empty=false | 内部 Text 重复更新不分叉；用户 Text 明确 overwrite；共享/外部 Text 拒绝；空正文明确 allow_empty，保留 Text |
| layout | 无 | 只读 Frame/节点/连线/正文摘要，不自动重排 |

名称不能为空或含 NUL，最大 255 UTF-8 字节；Blender 截断新名字时撤销新 Frame 并报错。nodes 为 1..512 个精确节点名；重复名字幂等跳过。原生正文最多 16,384 字符；不删除已有 Text、不替用户清理孤立数据。归组后 local location 可以改变，画布位置不变；嵌套 Frame 使用 parent 累积偏移换算，不假定相对坐标等于画布坐标。自动 shrink 的可见边框/尺寸由 Blender 绘制刷新，关闭 shrink 可固定注释布局。

## 独立扩展与可选适配器

源码仅位于：`D:\AI\MCP\Blender\Mod\blender_mcp_node_notes\repository\blender_mcp_node_notes`。

扩展本地仓库：`Mod/blender_mcp_node_notes/repository`，仓库 module `mcp_node_notes_local`；模块完整名称 `bl_ext.mcp_node_notes_local.blender_mcp_node_notes`。从 Blender 本地扩展仓库加载，不复制源码到 MCP 包或 Blender 安装目录，不依赖 Node Annotator。manifest 声明 Blender >=5.2；本项目实测具体版本为 5.2.1。

MCP 可选工具以 `blender_node_note_` 开头：

| 后缀 | 参数 |
|---|---|
| status | 无；在 Blender 检查实际版本、加载路径、注册状态与 RNA operators |
| extension | enable=true, persist=false；仅启用/禁用上述固定本地仓库，无任意路径加载 |
| create | group, frame, title='', body='', location=(0,0) |
| get | group, frame |
| update | group, frame, body, title=null, allow_empty=false |
| delete | group, frame, confirm=false, remove_text=false |

`extension(persist=true)` 仅允许当前 CONFIG 位于项目的 MCP 或 Mod 下，保存该受控偏好文件；用户配置目录不在这些路径时返回 unauthorized_config，不猜测环境变量或越界安装。persist=false 为当前会话启用；安装/启用不删除工程内容。源码更新后应保存自己的工作并重启 Blender，以加载完整扩展模块；不要把 import 缓存当作新版已加载。

未安装/禁用返回 `available=false, code=dependency_missing`；版本/API/path 不符为 unsupported_version。插件专属读写工具走 MCP isError + 结构化 CommandError，附版本/解决建议。原生 Frame 工具、Geometry Nodes、形态键和驱动器不依赖扩展。所有命令经过原有 command 分派、错误包装和 command_start/end 审计；MCP server 不 import bpy 或扩展。

Frame/Text 注释存入 .blend，自身 UUID 标识，不给普通 Frame 打标记。每个注释默认独立 Text；更新拒绝多 Frame 共享/非所有者/只读文本。扩展正文最多 10,000 字符、200 行，标题最多 128 字符单行。侧栏 Load/Apply 对明确目标操作；多行用现有 Text Editor 编辑，不弹不可控临时窗口。画布正文来自原生 `frame.text`，不是 tooltip 或面板替代品。删除须 invoke 二次确认或明确 confirm=true，默认保留 Text；清理 Text 仅限显式 remove_text=true。

## 可读性约定与验证

主流采用左 Input → 中 Process → 右 Output；用短标题 Frame 分隔参数派生、整理、算法、部件与组合，正文简洁分行说明输入/处理/输出。保留间距，减少交叉和回绕；复杂复用优先考虑节点组。专用工具只编辑指定目标，绝不自动整理旧图；旧椅子/花瓣灯 .blend 均不修改。

最小检查命令（MCP 根目录执行）：

```powershell
.\.venv\Scripts\python.exe tests/test_node_frames.py
.\.venv\Scripts\python.exe tests/test_node_frames_atomic.py
.\.venv\Scripts\python.exe tests/test_node_frames.py --live
.\.venv\Scripts\python.exe ..\Mod\blender_mcp_node_notes\tests\test_notes_stub.py
.\.venv\Scripts\python.exe ..\Mod\blender_mcp_node_notes\tests\test_mcp_adapter.py
```

完整插件实机运行器：

```powershell
.\.venv\Scripts\python.exe ..\Mod\blender_mcp_node_notes\tests\run_validation.py
```

2026-10-04 已执行：3 项 MCP 加载/旧 47 工具 schema 兼容检查、9 项原生 Frame 失败注入、53 项插件 stub、108 项 Blender API、21 项 GUI、12 项官方 MCP SDK 插件适配检查全部通过，manifest 实机校验通过。原生工具最终在临时禁用插件时完整 20 项通过，测试后恢复启用；直接在已启用状态运行时为 18 项，另两项只适用于缺失状态。12 项适配检查另覆盖禁用时原生访问与 dependency_missing。既有验证 58 项、场景 22 项、启动 16 项也通过，未无条件重跑整个仓库。

真实 Blender 插件/API/画布/保存重开验证由 `Mod/blender_mcp_node_notes/tests` 的运行器执行；其专属报告、PNG、.blend 位于 Mod 插件自己的 .runtime。普通 Frame 报告位于 MCP/.runtime/frame_acceptance.json。GUI 图像在 SpaceNodeEditor POST_PIXEL 绘制回调中读取实际 GPU framebuffer，再用 Blender Image.save 保存；timer 上下文截图曾产生 1×1 PNG，不能当作验收证据。最终 1178×726 画布与 MCP Notes 侧栏 PNG 已检查，Frame 三行正文可见。没有使用 computer-use、鼠标/键盘或桌面抓屏。

最终证据：Mod 插件 `.runtime/validation_summary.json`、`blender_api.stdout.log`、`mcp_adapter.json`、`canvas_18891dbb/report.json` 与同目录两张 PNG/verified.blend。受控 CONFIG 为 MCP/.runtime/blender_config；启动器把官方 BLENDER_USER_EXTENSIONS 指向 Mod 插件 .runtime/blender_extensions，仅缓存放那里，源码仍由上述固定仓库直接加载。

扩展新建注释使用 label_size=24、shrink=false、520×220 边框，避免默认字号 0 隐藏正文；长正文仍需用户调整边框/简化内容。显式 remove_text 清理会排除 Text 默认 fake user，但任何额外真实引用仍拒绝。删除失败补偿可能重建 RNA 对象，保持逻辑名称/UUID/内容而非旧指针；恢复失败如实报告 rolled_back=false。确认弹窗未人工点击，明确 confirm=true 与无确认拒绝已实测。当前目标只含 bpy.data.node_groups；其它树仅参与共享审计。其它 Blender/平台未实测。

当前源码的官方 stdio 工具列表为 60（新增 7 个原生 + 6 个可选工具）；本聊天缓存仍是先前的 42 个，需刷新/重启 Codex 才显示新 schema。GUI bridge 已加载全部 56 个命令，扩展已从 Mod 启用；不需要重复安装 Codex 插件。已有 4 个 Project 模型 SHA256 不变，没有提交或推送。

## 官方依据与体验参考

- [Blender 5.2 NodeFrame API](https://docs.blender.org/api/5.2/bpy.types.NodeFrame.html)：text 是 Text 指针，shrink、label_size。
- [Node API](https://docs.blender.org/api/5.2/bpy.types.Node.html)：location 相对 parent；location_absolute 是画布位置。
- [Text API](https://docs.blender.org/api/5.2/bpy.types.Text.html)：from_string/as_string。
- [Frame 手册](https://docs.blender.org/manual/en/5.2/interface/controls/nodes/types/layout/frame.html)：正文直接绘于 Frame，Text Editor 编辑。
- [本地扩展仓库](https://docs.blender.org/manual/en/5.2/editors/preferences/extensions.html)、[部署扩展](https://docs.blender.org/manual/en/5.2/advanced/deploying_blender.html)：本地仓库可指定目录。
- [Node Annotator](https://extensions.blender.org/add-ons/node-annotator/)、[版本历史](https://extensions.blender.org/add-ons/node-annotator/versions/)仅作为创建/编辑 Frame 注释的体验参考；其公开说明提到侧栏和新 Text Editor 窗口，本实现选择复用现有编辑器。没有下载、安装或复制其源码；兼容声明不等于本机实测。
