# 私有文件快照示例

在插件页安装本目录，审阅权限后启用。允许“新建自身私有文件”后调用 `worker_snapshot:export`（无参数），返回 `operation_ref`、`name` 和 `root_ref`。使用 `worker_snapshot:status`，参数为 `{"operation_ref":"导出返回的引用"}`，确认 `state:completed`、`effect:committed` 后导出才算完成。

每次导出生成唯一 ASCII 文件名，内容为 Host API `runtime.info` 返回的 JSON。文件写能力只允许在当前安装的固定私有空间新建文件，不覆盖、不删除，不隐含读取许可。另行允许读取后可用公开 `files.stat/read/list` 获取文件；任务结果仅在提交时和查询时均有有效读取许可时提供。

首轮文件能力限 Windows x64、本地 NTFS；单文件 8 MiB，块 32 KiB。更新同一安装保留文件，重新安装使用新空间；旧运行的引用不能继续使用。插件仍以当前系统用户运行，Host API 私有空间不等于 Python 的操作系统沙箱。

可使用 `python packaging/build-eclplugin.py examples/plugins/worker_snapshot build/worker_snapshot.eclplugin` 制作归档。本示例无第三方依赖。
