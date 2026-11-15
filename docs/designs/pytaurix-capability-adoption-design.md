# PyTaruiX 能力承接实施文档：图片协议传输与自维护逻辑替代

状态：**阶段 1、3 已实施并通过桌面验收（2026-11-15）；阶段 2、4 未开始**

## 1. 背景与目标

ECL 桌面前端已运行在 PyTaruiX 宿主上（`ECL/desktop/tauri.py`）。本方案利用 PyTaruiX 已绑定但 ECL 尚未使用的能力，替代启动器中的自维护逻辑：

1. **图片传输**：以自定义 URI scheme 协议替代「base64 Data URL + JSON IPC」管线（本轮主项）。
2. **二进制 Channel**：作为流式/大载荷场景的预留能力，本轮只落基础设施，不迁移业务。
3. **单实例与窗口几何记忆**：评估用 PyTaruiX 官方插件替代自维护实现；调研发现存在行为冲突，见第 5 节决策点。

非目标：本轮不改插件 SDK 对外契约、不迁移背景视频流、不动 headless 模式。

## 2. 现状

### 2.1 图片管线（被替代对象）

- 后端：`ECL/api/bridge.py`（`ImagePolicy`、`_read_image_data_url` LRU、`_encode_image_bytes`）+ `ECL/api/files.py`（`image_fetch_data_url`、`image_read_file`、`image_save_url` 等）。远程图片先落盘缓存（`ECL_data/cache/remote-images`，TTL 7 天、128 文件、96 MiB 上限），再 base64 编码经 JSON IPC 返回。
- 前端：`frontend/src/api/client/fs.ts` 用 Map 缓存 dataUrl；`useTheme.ts` 对 >100 KiB 的 dataUrl 再转 blob URL。
- 代价：base64 体积膨胀约 33%、JSON 大字符串序列化、Python/前端双层缓存、解码在 JS 侧。

### 2.2 PyTaruiX 已有能力（ECL 未使用）

- **自定义协议**：`create_protocol` + `build(protocols=...)`。同步处理器在工作线程执行，返回 `ProtocolResponse`（状态码、头、字节正文）；异常归还空 500；未初始化归还 503。Windows Origin 为 `http://<scheme>.localhost/<path>`。
- **二进制 Channel**：`pytaurix.ipc.Channel` 支持 JSON 与 ArrayBuffer，配额流控。
- **window-state 插件**（需构建时 `plugin-window-state`）：窗口几何自动保存/恢复，`flags`/`denylist`/`skip_initial`，状态文件在应用配置目录。
- **single-instance 插件**（需构建时 `plugin-single-instance`）：第二进程启动时回调 `(app_handle, argv, cwd)`，随后第二进程退出。

## 3. 方案设计：ecl-img 图片协议

### 3.1 总体结构

沿用 `background_video_open` 已验证的「受控 URL + 随机令牌」模式：

```
前端 invoke image_url_resolve {kind, path|url, refresh?}
    → 后端校验（本地路径边界 / 远程 URL 规整）/ 命中磁盘缓存
    → 签发 URL  http://ecl-img.localhost/<token>/<digest>.<ext>
    → 前端 <img :src="url">，浏览器直连协议处理器取字节
```

新增模块 `ECL/api/image_protocol.py`（协议处理器 + 令牌表），`ECL/desktop/tauri.py` 的 `build(...)` 增加 `protocols=[create_protocol("ecl-img", handler)]`。

### 3.2 后端改动

| 位置 | 改动 |
| --- | --- |
| `ECL/api/image_protocol.py`（新增） | 协议处理器：校验令牌 → 解析 digest/扩展名 → 读取缓存文件或按需触发下载 → 返回字节 + `content-type` + `etag`（mtime/size 摘要）+ `cache-control: no-store`（协商由 ETag 承担，避免前端 stale）+ 处理 `If-None-Match` 返回 304；`fetch` 场景回显 `access-control-allow-origin`（仅匹配页面 Origin 白名单） |
| `ECL/api/files.py` | 新增 `image_url_resolve` 命令：本地路径经 `_normalize_file_path` + `resolve()` + 符号链接/junction 边界校验后注册令牌；远程图片复用既有 `_fetch_remote_image` 合并与校验逻辑，返回协议 URL 而非 dataUrl |
| `ECL/api/bridge.py` | `ImagePolicy` 保留（MIME 映射、大小上限共用）；`_read_image_data_url` 与 `_encode_image_bytes` 的 LRU/编码随调用方迁移逐步移除 |
| `ECL/desktop/tauri.py` | `build(...)` 注册 `ecl-img` 协议；`launcher:closing` 时清空令牌表 |

令牌表：`dict[token, ImageTokenBinding]`，绑定 kind（remote/local）、digest、扩展名；进程内随机令牌（`secrets.token_urlsafe`），TTL 与上限（防累积），页面刷新后重新 resolve 即可。**协议处理器不接受任何客户端提供的路径**，只接受令牌 → digest 映射，杜绝路径遍历（AGENTS 5.2）。

### 3.3 前端改动

- `fs.ts`：`readImage` 改为 `image_url_resolve` → 返回协议 URL；移除 dataUrl Map 缓存与 revision 逻辑（HTTP 缓存由 ETag/304 承担）。
- `useTheme.ts`：移除 `dataUrlToBlobUrl` 补丁路径。
- 背景图（settingsStore）、皮肤/披风展示、实例图标：改用协议 URL。
- **保留 dataUrl 的两处**（阶段 3 再评估）：
  - `plugin-sdk/api.ts` 的 `image_fetch_data_url`：插件 SDK 对外契约，本轮不动。
  - `useSkinModelDetector`：需要 canvas 像素访问，跨源 canvas 需 CORS 头，先保留 dataUrl。

### 3.4 二进制 Channel（预留，阶段 2）

本轮仅在 `ECL/api/` 落一个通用 `stream` 通道辅助设施（Channel 创建、配额、关闭语义），不接业务；供后续大文件预览、远程图片渐进加载等场景使用。避免与协议方案争抢职责：`<img>`/`fetch` 走协议，主动字节流走 Channel。

## 4. 单实例与窗口几何记忆评估

### 4.1 单实例：存在行为冲突，建议保留现有实现

| 维度 | 现有 `SingleInstanceService` | PyTaruiX single-instance 插件 |
| --- | --- | --- |
| 隔离粒度 | 按数据目录（发现文件在 `data_path`），不同数据目录可并行运行 | 按**应用标识**的系统级互斥，不同数据目录会被互斥 |
| 配置开关 | `single_instance: false` 可关 | 注册即在；可按配置条件注册，但关闭后第二进程会完整启动新 GUI |
| 第二进程探测时机 | bootstrap 阶段、GUI 栈加载前，轻量 | 第二进程需先完成 Python 导入 + Tauri 构建，唤醒更重 |
| headless/CLI | 显式绕过 | 需保证 headless 角色不注册插件 |
| 启动意图 | 令牌校验 + 有类型 `LaunchOptions`（focus/launch） | argv 直传，可用 `parse_launch_options` 转换，但无隔离判断时机 |

冲突点 1 是硬约束：插件互斥按应用标识全局生效，**多数据目录并行运行会直接失效**。结论：本轮不替换，保留现有实现；若未来确认不存在多数据目录并行场景，再按迁移契约单独立项。

### 4.2 窗口几何记忆：现状为休眠机制，插件替换是净收益

**现状核实（2026-11-15）**：现有持久化链路是断头路——

- 前端 `windowApi.updateBounds`（`features/windows/api/windowApi.ts`）**没有任何调用方**；全前端检索不到 `updateBounds` 的使用。
- 后端 `window_update_bounds`（`ECL/api/windows.py`）因此不会被触发；节流保存（`_schedule_window_bounds_save`）、退出补写（`_flush_window_bounds_on_exit`，订阅 `launcher:closing`）全部处于休眠。
- 后端窗口事件回调（`bridge.py`、`windows.py`）只处理 destroy/close，不监听 `WindowEvent.Moved/Resized`。
- `ui.windows` 配置节唯一写入者是 `window_update_bounds` → 实际无人写入；`window_open` 读取 `saved_bounds` 时命中不到新数据，动态窗口每次回退到 descriptor 默认尺寸。
- 主窗口几何从不持久化：每次启动按 `tauri` 配置的默认 900×600（minWidth 960）创建。

**插件方案**：注册 `window_state(flags=("size","position","maximized"), denylist=(...))`，Tauri 原生层在窗口创建时按 label 自动恢复、关闭/退出时自动保存，Python 与前端零参与，主窗口与动态窗口一并覆盖。

替换后的删除面（休眠代码随替换清退，符合 AGENTS 第 2 节"不为维持旧测试保留废弃实现"）：

- `window_update_bounds` 命令、`WindowBoundsRequest` 模型、registry/类型/测试同步移除；
- `_schedule_window_bounds_save`、`_write_window_bounds`、`_flush_window_bounds`、`_pending_window_bounds`、`launcher:closing` 的 bounds 补写订阅；
- 前端 `windowApi.updateBounds`、`WindowBounds` 类型、showcase transport 分支；
- `window_open` 中的 `saved_bounds` 读取与钳制逻辑（恢复交给插件）。

剩余代价（比原评估大幅缩小）：

1. **状态位置迁移**：几何从 ui 配置（本就无人写入，实际为空或历史残留）移到插件状态文件。因原链路从未生效，**不存在真实用户数据可迁移**——"一次性几何重置"实际不成立，无需种子迁移。
2. **恢复语义差异**：插件按 label 恢复，要求 label 跨会话稳定——singleton 窗口 label 即 `descriptor_id`（点转冒号），天然稳定；非 singleton 窗口 label 含随机 `instance_key` 后缀，**每次都是新 label，插件不会为其恢复**，与现状（按 descriptor_id 记忆）不同。处理方式：非 singleton 窗口加入 denylist，明确不参与持久化，符合"多实例窗口位置本就无稳定记忆语义"的现状。
3. **崩溃丢失**：插件按窗口关闭时机保存，进程崩溃丢最后一次调整；现状休眠机制同样无此保证，不构成回退。
4. **多显示器越界**：PyTaruiX 桌面验收未覆盖多显示器变化的恢复效果，替换后需在验收中真实验证一次。

## 5. 实施步骤

1. **阶段 1（图片协议）**：新增 `image_protocol.py` + `image_url_resolve` + tauri.py 协议注册；前端 `fs.ts`/`useTheme.ts`/背景图切换；删除对应 dataUrl 调用方。回归：本地图片、远程图片、刷新、404/损坏图、令牌过期、页面重载。
2. **阶段 2（Channel 预留）**：通用通道辅助设施 + 单元测试，不接业务。
3. **阶段 3（窗口几何）**：替换为 window-state 插件（详见 4.2：现状为休眠机制，替换是净收益）。singleton 窗口参与持久化，非 singleton 加入 denylist；删除 `window_update_bounds` 全链路休眠代码；ECL 打包启用 PyTaruiX `plugin-window-state` feature（依赖与打包清单同步更新）；验收含主窗口跨会话恢复与多显示器场景。
4. **阶段 4（收敛）**：清理 `_read_image_data_url`/`_encode_image_bytes` 无调用方残留；`image_fetch_data_url` 保留给插件 SDK 并在文档标注长期计划。

单实例：本轮不动（见 4.1）。

## 6. 测试计划

- 后端：`tests/` 新增协议处理器用例（令牌校验失败 403、digest 不存在 404、ETag/304、路径边界拒绝、令牌表 TTL/上限、closing 后 503 语义）；`image_url_resolve` 参数校验与本地路径符号链接用例；现有图片命令测试同步更新。
- 前端：`fs.ts`/`useTheme`/settingsStore 相关 Vitest 更新，showcase transport 同步。
- 桌面验收：真实 Windows WebView 下 `<img src="http://ecl-img.localhost/...">` 渲染、fetch + CORS、304 协商、页面刷新后重新 resolve；符合 AGENTS 第 6 节范围要求（协议属 IPC/资源边界变更，按全量收口执行受影响仓库质量检查）。
- 依赖构建变更（阶段 3）另按 PyTaruiX AGENTS 第 6/8 节核对 feature 转发与 wheel 内容。

## 7. 风险与待决策项

| # | 事项 | 说明 |
| --- | --- | --- |
| 1 | 单实例替换 | 行为冲突（多数据目录隔离），本轮不替换——如你确认无需多数据目录并行，可推翻此结论 |
| 2 | 窗口几何迁移 | 已按 4.2 实施；非 singleton 窗口当前不存在（内置与插件描述符均为 singleton），插件 denylist 留空 |
| 6 | **pytaurix wheel 分发** | window-state feature 需构建期启用；已本地重建 cp311 wheel（`PyTaruiX/target/wheels-windowstate/`）并装入 ECL venv，版本号仍为 0.1.0a1。**CI/干净安装若从 PyPI 取默认 wheel 会在启动时因 `unsupported_capability` 失败**——发布前需发布带 feature 的正式版本并同步 `pyproject.toml` 锁定来源 |
| 3 | `ecl-img` scheme 名 | 需为小写 ASCII，避开保留名；如对 URL 形态有偏好请提出 |
| 4 | 皮肤模型检测 | 暂保留 dataUrl；如要一并迁移，需协议处理器支持 CORS 回显并用 `fetch` + `createImageBitmap` 改写 |
| 5 | PyTaruiX 侧 API 拓宽 | 本方案全部用现有绑定即可，无需改 PyTaruiX；若阶段 2 后需要流式响应（如视频走协议），再评估 ProtocolResponse 分块能力 |
