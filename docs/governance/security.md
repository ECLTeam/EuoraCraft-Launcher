# 安全要求与覆盖边界

采用 [ASVS 5.0.0 固定源码](https://github.com/OWASP/ASVS/tree/v5.0.0/5.0/en)。本项目是桌面启动器，只采用适用条目；下面的测试索引表示存在相关证据，不表示已经满足整条标准或通过完整安全审计。

| 来源条目 | 业务范围与要求摘要 | 责任领域与证据入口 | 状态及局限 |
| --- | --- | --- | --- |
| v5.0.0-1.2.1、v5.0.0-1.2.2 | 外部内容使用适用编码，链接仅允许安全协议 | 前端；`src/utils/markdown.ts` 及 Markdown、更新弹窗测试 | 部分覆盖；WebView 导航须独立复核 |
| v5.0.0-2.2.1、v5.0.0-2.2.2 | 输入按业务结构校验，安全检查在可信服务边界执行 | API/插件；`tests/test_api_models.py`、`tests/test_plugin_network.py`、前端 API validation 测试 | 部分覆盖；类型声明不代替运行时校验 |
| v5.0.0-3.2.2 | 需要作为文本展示的内容使用安全文本呈现 | 前端；Vue 插值、Markdown 净化相关测试 | 部分覆盖；不将所有富文本都改为纯文本 |
| v5.0.0-6.1.3 | 认证路径及其控制一起记录和检查 | 账户；`tests/test_microsoft_auth.py`、`tests/test_accounts.py`、`tests/test_yggdrasil_auth.py` | 部分覆盖；认证提供方服务端控制不由本项目证明 |
| v5.0.0-8.2.1、v5.0.0-8.2.2 | 功能和具体资源访问受显式权限约束 | 插件；`test_plugin_grant_store.py`、`test_plugin_permission_simplification.py`、`test_plugin_java_install.py`、`test_plugin_public_queries.py`、`test_plugin_game_resources.py`、`test_plugin_modpacks.py` 及实例/游戏操作测试 | 部分覆盖；采用当前基础、游戏、联网、Java 和明确选定账户的独立准入；安装计划与结果不跨撤权复活，受控 API 授权不等于进程沙箱 |
| v5.0.0-16.2.5 | 日志按敏感程度限制凭据等数据 | 日志/账户；`tests/test_operation_logging.py`、`tests/test_account_audit.py` | 部分覆盖；需持续核对新增日志边界 |
| ASVS V10 授权服务器相关控制 | 启动器不实现 OAuth 授权服务器 | 账户维护者；微软登录实现与协议 | 对外部服务器自身实现不适用；客户端凭据和流程仍需验证 |

## 本项目的桌面补充边界

以下要求来自项目的数据安全和平台规则，不伪造 ASVS 编号：

| 边界 | 要求与证据入口 | 状态 |
| --- | --- | --- |
| 归档与文件路径 | 拒绝越界、保护符号链接/重解析点边界；整合包与 Core 文件测试 | 部分覆盖，Windows/POSIX 分别测试 |
| 文件事务与安装 | 原子替换、回滚和恢复；`test_instance_profile_transactions.py`、`test_modpack.py`、更新测试 | 部分覆盖，不删除用户旧文件来消除违规 |
| 插件与进程 | 输入参数、超时、取消和实际退出；插件与无头进程测试 | 部分覆盖；原生代码及直接系统调用不受 API grants 完全限制 |
| WebView 与导航 | 区分外部页面、IPC 来源及原生窗口权限 | 待调查；核对 host 配置及原生 WebView 设计，不仅查看 Tauri 模板 |
| 发布产物 | 源码提交、gitlink、打包平台及完整上传 | 现有 CI 回归与发布流程；本轮不自动签名或发布 |

## 依赖审计

Python 从干净目标环境读取已安装版本，由独立工具环境的 pip-audit 2.10.1 审计第三方包；源码包本身通过项目测试与人工评审，不送入 PyPI 漏洞库假装是公开发行包。报告保留实际版本清单和此排除边界。

Windows/Linux 的运行环境和完整开发/构建环境分别扫描。前端使用项目 pnpm 10 的 audit，分别记录生产依赖和完整依赖。新工具不改变锁定策略，不自动升级依赖、不忽略无修复版本或注册表错误。

扫描不能证明原生轮子内嵌库、Java、Minecraft、运行时下载资源和未验证平台没有漏洞。扫描工具报告跳过未知包时，检查失败并记录局限，不自动按零漏洞通过。

首次运行 Windows Python 运行依赖未报告已知漏洞；前端锁文件命中 DOMPurify、brace-expansion、undici 公告。版本命中与产品可利用性分别核对，用户已授权针对命中依赖定向修复。完整结果随实施记录更新。

## 问题与敏感报告

已确认普通问题按贡献指南查重和归并登记；不按内部测试分支或每条公告拆单。公开 Issue 仅描述必要且已公开的版本/可访问性问题，不上传利用载荷、凭据或个人文件。私密渠道尚未核实前不虚构邮箱、保证响应时间或自动向他人通知。
