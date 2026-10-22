# 私有文件按版本删除

只在调用者明确传入 `root_ref`、`name`、`expected_etag` 和保留的 `mutation_key` 时提交删除。
加载、卸载及停止插件均不扫描或自动清理文件。`filesystem.delete` 不包含读写权限；目标版本须由先前合法读取取得。

`delete` 返回接纳回执后，调用 `status(operation_ref)`。只有 `completed`、`committed` 且 `deleted=true` 表示原版本已实际移除，成功删除不可恢复。
`PLUGIN_FILE_DELETE_PENDING`、`effect=unknown` 表示内核可能仍在等待外部句柄释放；`cleanup_pending=true` 时继续查询，不换键重删。
后续清理完成可以使该标志变为 false，但不会把 unknown 改成成功。撤销许可后隐藏删除确认，安全的自身任务查询仍可用。

提交等待超时后，通过同一 `mutation_key` 查询 `mutations.get`；取消请求不等于任务已停止，接受提交后不能撤销。
仅支持 Windows x64 本地 NTFS、当前安装自身私有空间、单个普通单链接平铺文件和至多 8 MiB。
