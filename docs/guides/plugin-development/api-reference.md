# Worker 业务接口索引

本页覆盖当前 Host API 方法族；所有请求为 JSON，除明确可选字段外严格校验。具体枚举、配额、失败语义集中见[插件包协议](../../protocols/eclplugin-package-format.md)，不同宿主未接入服务时返回明确失败。

运行引用不是磁盘路径，不能跨 Worker、运行代次或安装复用。表中“基础”指启用后的内部 `plugin.base`。

## 基础控制、命令、设置和面板

| 方法                  | 主要输入                                   | 结果                                          |
| --------------------- | ------------------------------------------ | --------------------------------------------- |
| `runtime.info`        | `{}`                                       | API/协议版本、平台、Python ABI、运行状态      |
| `mutations.get`       | `mutation_key`                             | 原变更 pending/succeeded/failed 及结果/错误码 |
| `commands.register`   | 名称、callback_ref、输入输出字段、exported | command_ref、registration_ref                 |
| `commands.unregister` | 已登记注册引用                             | 解除自身注册                                  |
| `commands.call`       | command_ref、args                          | `value`，按已登记 schema 校验                 |
| `settings.define`     | 完整 definitions                           | schema_revision                               |
| `settings.get`        | keys                                       | values、revision                              |
| `settings.set`        | values、expected_revision                  | 实际新 revision                               |
| `events.subscribe`    | event、callback_ref                        | subscription_ref                              |
| `events.unsubscribe`  | subscription_ref                           | 解除自身订阅                                  |
| `panels.register`     | panels                                     | panel_refs                                    |
| `panels.list`         | `{}`                                       | 本运行面板定义                                |
| `panels.data`         | key                                        | 本面板 lists/tasks 数据                       |
| `plugins.list`        | `{}`                                       | 其他已启用插件导出的命令描述                  |
| `plugins.call`        | plugin、command、args                      | 目标命令的 value                              |

事件仅有自身设置和所属联机变化。面板数据回调和命令定义优先通过 SDK 包装方法注册；取消注册时使用宿主实际返回的引用，不自行生成。

## 资源与实例

| 方法                      | 主要输入                                   | 行为与结果                                                                    |
| ------------------------- | ------------------------------------------ | ----------------------------------------------------------------------------- |
| `resources.list`          | capability、cursor?、limit?                | 对应能力的运行引用列表；基础可发现自身私有根                                  |
| `instances.scan`          | root_ref、force?、cursor?、limit?          | 获准根中的实例引用和摘要                                                      |
| `instances.get`           | instance_ref                               | version_id、alias、loader、game_version、profile、profile_etag、running_count |
| `instances.profile_patch` | instance_ref、expected_profile_etag、patch | 管理游戏；所属任务回执                                                        |

游戏范围由主窗口选择根目录。资料 patch 只接受 alias、description、favorite、tags；并发 etag 冲突不得自动强行覆盖。其他启动设置、图标和存档不属于该入口。

## 游戏安装和进程

| 方法           | 主要输入                                                      | 结果                                 |
| -------------- | ------------------------------------------------------------- | ------------------------------------ |
| `game.catalog` | type?、cursor?、limit?                                        | release/snapshot 固定目录            |
| `game.loaders` | game_version、loader、cursor?、limit?                         | 与游戏版本匹配的加载器版本           |
| `game.install` | root_ref、version_id、version_name?、loader?、loader_version? | 所属安装任务，成功结果为实例摘要     |
| `game.launch`  | instance_ref、options?                                        | 所属启动任务，成功结果为 GameRunView |
| `game.running` | instance_ref                                                  | 仅本运行创建的游戏记录               |
| `game.stop`    | game_run_ref                                                  | 所属停止任务及实际退出结果           |

目录查询为基础能力，其余使用 `instances.manage`。支持 vanilla/Fabric/Forge/NeoForge/Quilt，省略加载器版本时选择兼容版本，不接受任意下载 URL。

启动 options 允许 memory_mb 512—65536、width 320—16384、height 240—16384、fullscreen，以及获准 `runtime_ref`。账户和其余启动准备由宿主提供。直接启动入口不提供裸 Java 路径、wrapper、脚本、任意参数或环境变量；[启动钩子](extensions.md)另有独立的有限修改规则。

GameRunView 包含 game_run_ref、instance_ref、pid、running、exit_code。停止按原始所有者与进程对象复核，不能按 PID 查杀其他来源。

## Java、账户和公共配置

| 方法                | 主要输入                                       | 能力与结果                                         |
| ------------------- | ---------------------------------------------- | -------------------------------------------------- |
| `java.inventory`    | cursor?、limit?                                | java.read；所选登记摘要，不扫描磁盘                |
| `java.validate`     | runtime_ref、required_major?                   | java.read；固定参数的单目标探测                    |
| `java.catalog`      | major_version?、runtime_kind?、cursor?、limit? | java.catalog.read；固定官方包引用                  |
| `java.install_plan` | package_ref                                    | java.install；plan_ref 与计划摘要                  |
| `java.install`      | plan_ref                                       | java.install；所属安装任务，成功后可选用返回运行时 |
| `accounts.list`     | cursor?、limit?                                | accounts.read；仅所选账户摘要                      |
| `accounts.get`      | account_ref                                    | 单项账户摘要                                       |
| `config.public`     | `{}`                                           | 基础；语言、主题、强调色、默认内存                 |

已登记 Java 的使用按运行准入执行；运行引用不提供全局删除能力。账户摘要不包含邮箱、令牌、真实宿主账户 ID 和提供方私有数据。没有任意配置分区读取或通用配置写入口。

## 模组、资源包与光影

以下 `<kind>` 为 `mods`、`resourcepacks`、`shaders`。

| 方法                   | 输入摘要                                                     | 结果与边界                              |
| ---------------------- | ------------------------------------------------------------ | --------------------------------------- |
| `<kind>.list`          | instance_ref、cursor?、limit?                                | 查看范围中的 resource_ref、名称、大小等 |
| `<kind>.install`       | instance_ref、source: FileRef、filename                      | 管理范围；从私有普通文件提交新资源      |
| `<kind>.delete`        | resource_ref                                                 | 删除原列表对应的确切文件版本            |
| `mods.set_enabled`     | resource_ref、enabled                                        | 设置明确目标状态，不是反复切换          |
| `<kind>.search`        | source?、query?、game_version?、loader?、offset?、limit?     | Modrinth/CurseForge 公开项目摘要        |
| `<kind>.versions`      | source?、project_id、game_version?、loader?、cursor?、limit? | 公开版本分页                            |
| `<kind>.download_info` | source?、project_id、version_id                              | 公开文件信息，不自动下载或写入  |

查看使用 instances.read 或相同管理范围；写入使用 instances.manage。下载信息查询为固定来源基础能力，实际网络下载仍单独批准站点。实际目录沿用实例隔离/共享规则，不能根据名称猜路径。

## 整合包

| 方法                      | 输入摘要                                                                      | 结果                               |
| ------------------------- | ----------------------------------------------------------------------------- | ---------------------------------- |
| `modpacks.prepare`        | source: FileRef                                                               | 准备任务，成功返回 preparation_ref |
| `modpacks.get`            | preparation_ref、cursor?、limit?                                              | 格式、版本、加载器和文件预览       |
| `modpacks.install`        | root_ref、preparation_ref、version_name、excluded_file_ids?                   | 获准根中新实例安装任务             |
| `modpacks.export_preview` | instance_ref                                                                  | 导出预览任务，返回 preview_ref     |
| `modpacks.export_files`   | preview_ref、cursor?、limit?                                                  | 候选文件分页，不自动全选           |
| `modpacks.export`         | preview_ref、selected_file_ids、destination: FileRef、name、version、summary? | 明确选择文件导出到私有新 .mrpack   |

导入与导出按运行准入执行；每个下载地址单独经过重定向、DNS 与连接检查。取消可能保留共享基础安装，按真实 partial/committed 结果处理，不承诺全流程回滚。

## 私有文件与下载

FileRef 为 `{root_ref, relative_path}`，root_ref 通过基础资源发现获取。私有空间不接受任意用户路径、子目录或设备名称。

| 方法                  | 输入摘要                                       | 结果                         |
| --------------------- | ---------------------------------------------- | ---------------------------- |
| `files.stat`          | target                                         | 大小、时间、etag 等 FileView |
| `files.list`          | directory、cursor?、limit?                     | 私有根文件列表               |
| `files.read`          | target、offset?、count?、expected_etag?        | Base64 数据、eof 和版本信息  |
| `files.write_begin`   | target、total_bytes、overwrite:false           | write_ref；新建不覆盖        |
| `files.replace_begin` | target、expected_etag、total_bytes             | 按原文件版本准备替换         |
| `files.write_chunk`   | write_ref、offset、data_b64                    | 连续分块上传进度             |
| `files.write_commit`  | write_ref、sha256                              | 所属提交任务回执             |
| `files.write_abort`   | write_ref                                      | 上传中止或已交任务的释放情况 |
| `files.delete`        | target、expected_etag                          | 按原文件版本删除任务         |
| `network.request`     | method:GET/HEAD、url、headers?                 | 获准精确站点的有界响应       |
| `downloads.submit`    | target、url、overwrite:false、expected_sha256? | 网络加私有提交的所属任务     |

文件和私有下载单文件上限 8 MiB，块最大 32 KiB，安装空间 64 MiB。响应和任务配额详见包协议。资源安装的 64 MiB 领域上限不能解除私有源文件的 8 MiB 限制。

重定向逐跳校验，内网与特殊用途地址防护保留。认证头仅允许 HTTPS；没有 POST、请求体、任意 Socket 或 HTTP 客户端对象。Python 自身 OS 能力不受 Host API 限制。

## 自身任务

| 方法                | 输入摘要        | 结果                                             |
| ------------------- | --------------- | ------------------------------------------------ |
| `operations.get`    | operation_ref   | 本运行 TaskView                                  |
| `operations.list`   | cursor?、limit? | 自身任务分页                                     |
| `operations.cancel` | operation_ref   | requested、state、effect；请求成功不代表实际停止 |

TaskView 含 state、percent、stage、message、can_cancel、cancellation_requested、effect、cleanup_pending、result、error_code。结果每次交付都复核原始准入，失权后 result 可为 null，不能据此认定业务未产生效果。没有通用 operations.submit。

## 界面与扩展方法

`ui.register_html/vue` 见[插件界面](frontend.md)；`ui.windows.open/get/focus/close` 在当前运行时一律返回 `PLUGIN_UI_ISOLATION_UNAVAILABLE`，独立窗口隔离不再提供。`hooks.register`、`crash.register`、`compat.register`、`tools.register/run/stop`、`auth.register`、`connector.*` 见[扩展开发](extensions.md)。这些方法均只开放已接线能力，不透传宿主全局命令表。
