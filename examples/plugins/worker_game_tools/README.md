# 游戏资源与整合包 Worker 示例

将本目录按 [v2 包协议](../../../docs/protocols/eclplugin-package-format.md)安装到 Windows x64 启动器。启用时只注册 `request` 命令，不扫描目录、下载、安装或启动游戏。查看游戏、管理游戏、所选 Java、账户及网络范围分别在可信授权界面批准；未批准的可选能力明确失败。

`request` 的参数为 `method`、`params_json` 和 `mutation_key`，输出 `json`。其中 `params_json` 是下面参数对象的 JSON 字符串。相同变更重试保留原去重键，新的操作换一个键。所有引用只在本次启用期间有效，不能填写磁盘路径或宿主持久资源 ID。

| 操作 | method 与参数 |
| --- | --- |
| 查找已批准根 | `resources.list`：`{"capability":"instances.manage"}` |
| 列出实例并由用户选择 | `instances.scan`：`{"root_ref":"上一步 root_ref"}` |
| 查找私有文件根 | `resources.list`：`{"capability":"plugin.base"}` |
| 搜索在线模组 | `mods.search`：`{"query":"sodium","game_version":"1.21.1","loader":"fabric"}` |
| 查看平台版本 | `mods.versions`：`{"project_id":"搜索结果 project_id","game_version":"1.21.1","loader":"fabric"}` |
| 获取公开文件信息 | `mods.download_info`：`{"project_id":"项目标识","version_id":"平台版本标识"}`；下载使用 `downloads.submit`，站点必须在清单和批准范围内 |
| 查看已安装模组 | `mods.list`：`{"instance_ref":"已选择实例引用"}` |
| 从私有文件安装 | `mods.install`：`{"instance_ref":"实例引用","source":{"root_ref":"私有根引用","relative_path":"downloads/example.jar"},"filename":"example.jar"}` |
| 明确禁用模组 | `mods.set_enabled`：`{"resource_ref":"列表返回的资源引用","enabled":false}`；重复禁用不会重新启用 |
| 删除明确版本 | `mods.delete`：`{"resource_ref":"列表返回的资源引用"}`；原文件变化后重新读取列表，旧引用不能删除替换文件 |
| 识别整合包 | `modpacks.prepare`：`{"source":{"root_ref":"私有根引用","relative_path":"downloads/example.mrpack"}}` |
| 查询准备结果 | `operations.get`：`{"operation_ref":"所属任务引用"}`；完成后取得 `preparation_ref` |
| 查看整合包预览 | `modpacks.get`：`{"preparation_ref":"准备引用","limit":25}`；用 `next_cursor` 继续分页 |
| 导入新实例 | `modpacks.install`：`{"root_ref":"管理游戏根引用","preparation_ref":"准备引用","version_name":"example-pack","excluded_file_ids":[]}` |
| 导出预览 | `modpacks.export_preview`：`{"instance_ref":"实例引用"}`；任务完成后取得 `preview_ref`，`modpacks.export_files` 可分页列出候选 |
| 导出明确文件集合 | `modpacks.export`：`{"preview_ref":"预览引用","selected_file_ids":["预览中明确选择的文件 ID"],"destination":{"root_ref":"私有根引用","relative_path":"exports/example.mrpack"},"name":"Example","version":"1.0","summary":"Example export"}` |
| 公共偏好 | `config.public`：`{}`；只返回语言、主题及默认内存白名单 |
| 已批准账户摘要 | `accounts.list`：`{}`；账户必须单独明确选择，不提供邮箱或凭据 |
| 取消自身任务 | `operations.cancel`：`{"operation_ref":"本运行任务引用"}`；查询实际状态和 `cleanup_pending` 后才判断结束 |

资源包和光影分别使用 `resourcepacks.*`、`shaders.*`，文件为 `.zip`，不提供模组启停。资源使用实例实际隔离目录，同名文件不会覆盖。私有文件可使用现有 `files.write_begin/chunk/commit` 上传，或使用 `downloads.submit` 下载；详细参数以协议为准。

整合包文件 URL 逐项检查批准站点，未批准时明确失败。导入期间可能已安装共享基础版本；取消和失败按真实副作用报告，不承诺撤销已经提交的基础版本或删除用户文件。导出只写插件私有新文件，不接受任意主机目标路径。
