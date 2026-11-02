# 排障与验证

## 错误处理

捕获 `HostApiError`/`ProtocolError` 并检查稳定 error_code。错误提示不一定含原始异常或路径；日志也不应输出完整命令、账户数据、认证头、令牌或隐私路径。

| 错误码或状态                                              | 处理                                                                            |
| --------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `PLUGIN_GRANT_REVIEW_REQUIRED`                            | 在可信主窗口审阅当前内容                                                        |
| `PLUGIN_REQUIRED_PERMISSION_DENIED`                       | 批准必要能力及有效资源，或调整插件功能声明                                      |
| `PLUGIN_DEVELOPMENT_REVIEW_REQUIRED`                      | 开发声明、依赖或原授权已变化；用 `--no-plugin-dev` 重启并审阅，再以开发模式重启 |
| `PLUGIN_DEVELOPMENT_SESSION_EXPIRED`                      | 开发会话已关闭，不能复用旧运行                                                  |
| `PLUGIN_DEVELOPMENT_FILES_UNSTABLE`                       | 文件仍在写入；等待稳定后再次保存或明确重试                                      |
| `PLUGIN_CONTENT_CHANGED`                                  | 执行内容或目录身份不符，重新检查实际安装目录及审阅状态                          |
| `PLUGIN_REVISION_CONFLICT`                                | 重新读取配置，由用户决定修改，不能自动覆盖                                      |
| `PLUGIN_SETTING_SCHEMA_CHANGED`                           | 配置定义或代码内容已变化，重新读取当前设置                                      |
| `PLUGIN_SETTING_SCHEMA_UNAVAILABLE`                       | 当前内容没有可用定义，先审阅并启用                                              |
| `PLUGIN_SETTING_TYPE_MISMATCH`                            | 修正定义或明确处理用户旧值，保留原配置文件                                      |
| `PLUGIN_STORAGE_FAILED`                                   | 检查文件损坏、权限和磁盘，不将失败当成首次运行                                  |
| `PLUGIN_DEADLINE_EXCEEDED` / `PLUGIN_OUTCOME_PENDING`     | 等待超时；查询原 mutation_key 或 operation_ref                                  |
| `PLUGIN_HOST_CLEANUP_PENDING` / `PLUGIN_JOB_STOP_PENDING` | 真实资源未结束；继续观察/重试清理，不同时启动新 Worker                          |
| `PLUGIN_API_METHOD_NOT_FOUND`                             | 方法未开放或当前宿主未接入服务，按手册核对拼写与能力                            |
| `PLUGIN_UI_ISOLATION_UNAVAILABLE`                         | 原生运行时、引擎或必需控制不满足条件，不能绕过门禁                              |
| `PLUGIN_NETWORK_TARGET_DENIED`                            | 检查批准站点、重定向、DNS 及连接目标，不扩大为任意地址                          |

开发页面显示最近状态和错误码；查看启动器日志中的中文阶段和结果，定位“文件准备”“Worker 生命周期”“实际清理”等阶段。源码语法或启动失败不能靠清空账户/实例目录修复。

## 没有自动重载

1. 确认开启的是插件开发模式，且当前插件是目录安装并已经启用。
2. 使用“打开插件目录”，确认编辑实际安装目录，而非最初导入目录。
3. 检查 `supports_hot_reload` 是否为 false，文件是否属于明确排除的缓存/依赖构建目录。
4. 查看是否需要重新审阅、人工禁用、等待清理或文件尚未稳定。
5. 检查启动参数或可选配置；参数仅本次生效，文件配置需重启后生效，临时开发信任不跨进程恢复。

## 设置没有更新

统一设置、面板和独立前端使用相同 revision。先确认选择的是同一安装，再检查是否是旧 schema 或内容摘要。冲突时重新读取，不用默认值覆盖用户文件。

没有定义的插件显示空设置；定义尚未加载或内容不匹配会明确不可编辑。禁用不删除配置，重载也不重新生成安装身份。

## 取消与超时

提交后即使页面关闭或等待方取消，实际任务仍可能继续。先查原回执，再查任务。只有实际终态和 `cleanup_pending: false` 才能认定相应资源收尾完成；失权后 result 为空不表示 effect 未发生。

## 开发验收建议

- 最小插件能安装、确认启用、执行命令并编辑配置。
- 保存源码后新 Worker 生效，旧回调、事件、窗口与资源引用失效，配置和私有文件保留。
- 覆盖连续保存、语法错误、显式退出、用户禁用、权限/依赖变化和关闭开发模式。
- 覆盖并发配置写入、旧版本冲突、坏文件及设置类型改变。
- 前端验证 Classic/Folia、亮暗、最小及放大窗口、长名称、键盘焦点和操作可达性。
- 区分模拟、真实源码 Worker、原生窗口、真实游戏/网络及冻结产物；未执行或跳过的项目不能算通过。

本项目测试命令以根目录 [AGENTS.md](../../../AGENTS.md)第 6 节为准。真实平台/发行物门禁的环境与证据见[原生运行时](../../../native/pytauri-runtime/README.md)及[无头运行](../headless-runtime.md)。

Windows 源码启动器的可重复界面门禁为 `python tests/plugin_development_ui_host.py`：先构建 `frontend`，在含固定版本 Python Playwright 1.58.0 的工具环境运行。门禁加载生产前端和实际 Worker，使用独立数据/用户目录，验证配置保存、代码换代、显式退出、人工重载及关闭模式，保存 12 组主题/尺寸和六语言截图及结果到忽略的 `output/playwright/plugin-development/`。它不替代 Linux/macOS 或冻结发行物验收。
