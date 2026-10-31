# 获准游戏安装示例

加载插件仅注册命令。公开版本查询属于基础能力；安装仅需在可信主窗口批准管理游戏，并选定游戏根目录。安装仅支持 Windows x64 本地 NTFS。

1. 执行 `catalog`、`loaders(game_version,loader)` 查看公开版本。
2. 执行 `discover(capability="instances.manage")`，明确选择一个 `root_ref`。
3. 执行 `install(root_ref,version_id,version_name,loader,mutation_key)`，新实例名称必须不存在。
4. 用回执中的 `operation_ref` 执行 `status`；`cancel` 只请求取消，实际结束须继续查状态。

回执只表示接纳。重放相同提交须重用原 mutation_key；RPC 等待超时可通过公开 `mutations.get` 查询，不能换键自动重试。完成后的 InstanceView 还要求提交时的原始管理准入仍有效，否则 result 为 null。失败残留保留，只允许同一运行对同一身份和请求继续；停止插件后不恢复旧运行控制权。主窗口任务中心显示真实安装阶段和下载统计。

[接口与边界](../../../docs/protocols/eclplugin-package-format.md#获准游戏目录安装与进程控制)
