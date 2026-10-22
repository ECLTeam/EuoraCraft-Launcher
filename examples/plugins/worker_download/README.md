# 私有空间下载示例

API v2 独立 Worker 示例。加载插件只注册命令，不自行联网。

先将 plugin.json 的 `network.read.resource_refs` 改成实际需要的精确公网站点，重新审阅内容及权限；示例域名仅为占位，不是可下载测试服务。私有文件与 Worker 当前要求 Windows x64 本地 NTFS。

- `submit(url, name, mutation_key, sha256?)`：新建文件，至多 8 MiB；可指定期望摘要，返回任务和逻辑文件引用。
- `status(operation_ref)`：查询真实状态及效果；100% 且 completed 表示已经发布。没有独立读取许可时 size 为 -1。
- `cancel(operation_ref)`：只请求取消，继续查询直到终态；已接受原子发布会明确拒绝取消。

提交时自行保留非空 mutation_key；相同请求重用该键取得同一回执。提交 RPC 至多 5 秒，任务总截止 1800 秒。固定预留 8 MiB，空间不足就拒绝；不覆盖、不续传、不解压、不继承代理、账户或 Cookie，不自动重试。提交等待超时应按原 mutation_key 查询 `mutations.get`，不能换新键假设此前没有接纳。

正式边界和验收见 [下载任务方案](../../../docs/plans/plugin-download-task-implementation-plan.md)。
