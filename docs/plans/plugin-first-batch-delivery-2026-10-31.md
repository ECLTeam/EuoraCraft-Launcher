# 插件第一批交付与验收

日期：2026-10-31（Asia/Shanghai）。第一批已完成并集成原工作区本地 `main`。第二、三批尚未实施，未推送、打标签或发布。

## 交付范围

| 能力 | 当前结果 |
| --- | --- |
| Java | 登记快照、所选目标有界校验、官方目录、安装计划及所属安装任务；已安装目标可用于本运行游戏启动。计划和结果绑定原始授权，撤权后重新批准不复活旧引用 |
| 模组、资源包和光影 | 当前实例实际数据目录内的列表、私有文件安装、按文件版本删除；模组显式启停；固定平台搜索、版本和公开下载信息，下载仍独立检查站点许可 |
| 整合包 | 私有归档准备、预览、所属安装任务、导出预览、明确文件选择及私有 `.mrpack` 导出；清单下载逐项走插件网络边界 |
| 公共配置与账户 | 公开偏好白名单；账户只返回明确选择后的玩家摘要，UUID 校验并规范化，不返回邮箱、凭据或宿主账户 ID |
| 授权界面 | 游戏、Java、账户分别维护候选版本；Java 读、校验/选用、目录、安装及账户准入均明确可见；六语言、空范围、失效选择与保存反馈同步 |
| 示例和协议 | [Java 示例](../../examples/plugins/worker_java_query/README.md)、[资源与整合包示例](../../examples/plugins/worker_game_tools/README.md)、[现行协议](../protocols/eclplugin-package-format.md) |

同时修复本批真实整合包往返暴露的依赖丢失：导入副本保留完整启动描述和真实 Minecraft 父版本，导出不再把自定义实例名当作 Minecraft 版本。插件导入使用独占新暂存目录，保留原有同名暂存内容。

## 必要检查与定向回归

按用户“测试只需要跑最必要的那些”执行，没有运行全量 pytest 或 `pnpm check`，没有修改或跳过既有 CI。相同代码已经通过的检查不重复；合并新增主线改动后仅重跑相交的授权、Worker、前端边界与构建。以下分组有用例重叠，不相加作为总数。

| 分组 | 实际验证 |
| --- | --- |
| 代码质量 | 主仓库改动 Python 文件的 Ruff 检查和格式检查；前端改动文件的 Prettier、ESLint；`git diff --check` |
| 授权与 Worker 合并回归 | `test_plugin_grant_store.py`、`test_plugin_permission_simplification.py`、`test_plugin_permission_ipc.py`、`test_plugin_snapshot_verify.py`、`test_plugin_worker_timeouts.py`、`test_plugin_java_queries.py`、`test_plugin_java_install.py`、`test_plugin_public_queries.py`、`test_plugin_game_tools_worker.py`：122 通过，1 按平台条件跳过 |
| Java 接入 | 安装并登记、所有者预约、重复提交、撤权及重新批准、当前运行选择；`test_plugin_java_game_launch.py` 的所选 Java 传入和准备时文件替换拒绝：2 通过 |
| 资源文件 | 隔离/共享位置、同名保护、明确禁用、去重、旧文件引用拒绝和目录 junction 越界拒绝；`test_plugin_game_resources.py` 通过 |
| 整合包 | 私有 ZIP 导入/导出、未批准站点拒绝、归档穿越拒绝、原暂存保护及自定义实例名往返；修复后与直接领域消费方 `test_modpack.py`、`test_modpack_preparation.py` 合计 69 通过；相关 `test_modpack_export.py` 也已通过 |
| 公共查询与资源目录 | 字段白名单、明确账户选择、空范围和旧引用拒绝、目录损坏保留原文件、无隐式认证刷新；平台搜索/版本/下载信息及非法地址输入的定向测试通过 |
| 共享文件与网络消费方 | `test_plugin_host_files.py`、`test_plugin_network_api.py`、`test_plugin_download_api.py`：166 通过，覆盖 Broker 路由和私有源文件直接消费方 |
| 现有探测及架构 | Core 的 `test_java_probe.py`、`test_java_probe_process.py` 及主仓库 `test_architecture.py` 已通过；Core 未发生代码或 gitlink 变更 |
| 前端合并验证 | `pluginManagementApi`、`pluginPermissionValidation`、`PluginPermissionsModal`、`PluginLocalResourceSelector`、`plugin-sdk/state` 五个测试文件：52 通过；构建内 TypeScript 检查和 `pnpm build` 通过 |

Java 安装回归使用真实 ZIP、安装器、登记及任务生命周期，网络响应和版本探测由 fixture 提供；真实桌面中的登记与游戏启动使用本机已有 Java 25。没有把模拟下载结果描述为官方站点联网验收。

## 真实 Windows 验收

使用 Python/Tauri/WebView2 和构建后的生产前端，连接本次进程树所属窗口，实测原生窗口及 WebView 尺寸。数据、账户和 WebView profile 均独立；认证提供方使用合成 fixture，账户摘要仍经过真实账户服务。

授权界面完成 26 项记录：Classic/Folia × 亮/暗 × 960×600、1200×800、1440×1000，四组 Tab/Shift+Tab/Esc 与焦点恢复，四组 60% 卡片透明度及 12px 模糊，另五种语言在最小窗口，以及真实保存授权后启用 Worker。最后账户行与操作按钮均可到达，未出现横向溢出。Java 和账户起始不代替用户选择；失效旧选择可明确清除。

这次没有重新执行原治理全站 axe 扫描，新增区域的真实布局与键盘结果不计入原扫描总数，也没有扩展已有例外或新增风险豁免。

代表性游戏闭环通过真实桌面、源码 Worker 和实际 Java 进程执行：

1. 只读复制已有完整 Minecraft 缓存到隔离目录，不读取或复制用户存档及账户凭据。
2. 明确登记并选择实际 Java 25；插件上传 `.mrpack`、准备、导入自定义名称实例。
3. 选择导出文件并写入插件私有新包，重新准备后确认 Minecraft 版本保持 `26.3-pre-1`。
4. 插件使用所选 Java 启动真实 Minecraft，确认 854×480 游戏窗口、OpenAL 和纹理图集初始化。
5. 所属停止任务完成并确认进程实际退出；本次 Windows 终止退出码为 1，没有将退出请求提前当作退出成功。

合并主线后另完成一次生产包启动、权限界面显示和实际 Worker 公共查询验收。没有重复未受影响的整套主题矩阵。

本地证据保存在实施工作树的 `build/plugin-three-batches/` 与 `output/playwright/plugin-batch1/`，包括原生尺寸/交互记录、截图、构建日志及真实游戏结果；这些为本地验收产物，不提交到代码仓库。

## 本地交付和边界

- 主功能提交：`9093a15`；合并实施期间新增主线插件性能、生命周期和 CI 修复后，代码交付点为 `4db4bca`。
- 前端功能提交：`44fc2b3`；保留主线新增前端修复并合并后的本地 `main` 为 `b38aaad998250ca63f7fb666d139a8f7ddac58c4`。
- 已先快进原前端本地 `main`，再快进原主仓库本地 `main`。集成前两个原工作区均无无关改动，集成后工作区干净，父仓库 frontend gitlink 正确。该文档后续只增加交付记录提交。
- Core 仍为 `e8aacb50ca82b8719f91f4f88ed26f9477172b64`；Florolding 仍为 `80d037e73923e93cf961119932a117d3a04c297b`。
- 本轮没有构建新的 PyInstaller/Nuitka 发行物，没有将相关跳过项称为验证通过；完整发行及 Linux/macOS 验收仍属第三批。
- 第二批的面板/独立窗口、回调与扩展，以及第三批的跨平台、分发和签名尚未实施，不将第一批称为“插件全部完成”。
