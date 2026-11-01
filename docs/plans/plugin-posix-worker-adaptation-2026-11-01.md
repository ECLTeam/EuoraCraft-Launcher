# 第三批 Worker 平台适配实施细化

用户已批准三批总方案，并于 2026-11-01 明确要求开始适配。本文件细化批内接口与验收，不重复建立实施确认关卡。当前从独立工作树 `codex/plugin-posix-worker` 开发，保护主工作区中的其他任务。

## 范围与约束

本次适配 Linux/macOS Worker 的受信引导、本地通信、进程监管与受控文件操作，保持 Windows 原生 Job 和 NTFS 路径。跨平台自定义 WebView、在线分发和签名仍按三批方案的后续步骤实施。用户取消的 Nuitka 构建、关机要求继续取消；不发布版本。

不能把平台判断删除、模拟测试或跳过用例当作支持证据。Windows 本机没有已安装的 WSL 发行版；Linux/macOS 的真实验收需要对应环境或 CI。代码与工作流先完成本地可复核交付，未跑过的平台明确待验收。

## 接口与顺序

1. 将通用运行错误移到插件 API 的独立模块。Worker 引导继续只导入标准库和 `ECL.plugin_api`；不为平台接线导入桌面或业务服务。
2. 本地通道统一为有界 JSON 消息接口。Windows 保留 DACL 命名管道；POSIX 使用独占创建的 0700 目录及 Unix socket，拒绝 TCP、链接和错误所有者。Linux 使用内核 peer credentials，macOS 使用原生 peer PID/UID；不能用消息里的 PID 认证。
3. 启动参数统一使用 `--endpoint`，Windows 端点严格绑定运行标识，POSIX 端点同样绑定运行目录。同步替换旧参数、类型、测试和文档，不增加旧参数别名。握手使用 `is_supervised` 表达已检查的监管条件。
4. 进程接口实际调用方为 `WorkerSupervisor`。Windows 继续先分配 Job 再恢复；POSIX 增加专属受信守护角色，控制流与插件通道分开。守护角色在独立会话内建立 Worker 进程组，保留原始子进程身份，宿主控制流关闭时清理普通后代；不依赖宿主退出回调。持有身份期间不提前回收组首进程，避免进程标识重用后误杀其他来源。只监管本来源的普通进程树，不宣传为恶意 Python 的操作系统沙箱；主动脱离监管的行为不作为可支持工具行为。
5. 文件接口集中平台分派，Windows 保留已有模型和原子语义。POSIX 使用目录描述符、拒绝链接的相对打开、同设备身份和有界读写；元数据保留物理身份，更新使用暂存、刷新与原子发布。不能用普通路径检查后重新打开替代对象边界。无法保持数据保护的变更必须明确拒绝，不能先开放全部权限再留下空实现。
6. 管理器、SDK、受信入口、协议和示例同步使用当前平台契约；按实际能力验收更新支持表，其他原生平台能力继续明确拒绝。

## 验收与交付

- 通信：真实双向 RPC、错误 PID/UID 拒绝、错误运行目录与链接拒绝、限长、半包、超时、关闭和资源回收。
- 引导：批准前零插件语句；未加载宿主模块；代码变化拒绝；生命周期、命令、设置及重启可用；旧运行引用失效。
- 监管：普通子孙进程回收、宿主突然退出、启动失败、其他来源不受影响、保留未确认清理状态。
- 文件：创建、读取、替换、删除、冲突、撤权、取消、链接/硬链接拒绝、目录身份变化、清理失败与恢复；不能误删其他文件。
- 在 Windows 运行对应真实回归；Linux/macOS 使用真实 runner 执行同一基础行为用例，平台专属用例分别标明。此类共享契约和进程/文件边界修改完成后执行主仓库全量质量检查与 pytest。
- 提供三平台基础 Worker CI，检查失败、空覆盖与跳过必需平台用例不得当作成功。Nuitka 不执行；适用的 PyInstaller 验收单独记录。
- 本地测试、提交与主分支集成遵循 AGENTS.md；第三批远端推送按当前变更另行授权，第二批的推送授权不自动扩大为后续发布。

## 原生接口依据

- [Python 3.11 subprocess](https://docs.python.org/3.11/library/subprocess.html)：使用 `start_new_session` / `process_group`，不在多线程宿主使用 `preexec_fn`。
- [Linux Unix socket](https://man7.org/linux/man-pages/man7/unix.7.html)：本地端点与系统凭据。
- [Apple XNU Unix socket 定义](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/un.h)：macOS 原生 peer PID 选项。
- [wait/waitid](https://man7.org/linux/man-pages/man2/waitpid.2.html)：区分观察退出与回收身份，不把查询等同于资源已释放。


## 本地基础实现与验证记录

2026-11-01，本工作树已实现第 1—4 步，并增加第 6 步涉及的统一入口与基础行为测试。`PluginWorkerService` 不再重复硬编码 Windows，实际平台准入集中在进程监管工厂；未实现的文件和原生窗口能力仍由所属边界拒绝。Windows DACL 管道及执行前 Job 分配保留，POSIX 源码实现使用专属守护会话、私有 Unix socket、内核凭据和持有身份的退出观察。

真实平台工作流为 `.github/workflows/plugin-worker-platforms.yml`，分别运行三平台基础行为及对应原生边界，原始 JUnit 报告作为产物保存；必需用例无覆盖、失败或跳过均失败。基础源码与冻结发行物验收分开，当前工作流没有冻结构建步骤。

本机 Windows 检查记录：

- `ruff check ECL tests`、`ruff check . --output-format github`、`ruff format --check ECL tests` 均通过。
- 共用源码 Worker 门禁最终 113 项通过，7 项冻结或其相关参数用例明确不纳入本次源码门禁；Windows 原生监管门禁 5 项通过。两份报告均已检查非空、无跳过和无失败。
- 执行主仓库全量 pytest，得到 2688 项通过、49 项跳过及 1 项旧平台预期失败。该旧用例仍将 Linux x64 当作不支持，已改为验证 Windows ARM64、Linux RISC-V、macOS PowerPC 和未知系统的拒绝；所在测试模块及全部共用源码门禁复跑通过。其后仅调整相关测试和文档，不重复运行相同全量矩阵。
- POSIX 专属测试在本机 Windows 跳过，不能视作 Linux/macOS 支持证据；新增的 11 项 POSIX 原生用例须由对应 runner 实际执行。共享守护控制语义有本机单元回归，仍不能代替实际内核测试。
- 工作流 YAML 及内嵌 Python 已在独立工具 venv 中解析检查，未修改全局 Python 环境；`git diff --check` 通过。

当前交付为基础 Worker 平台引导与监管实现。第 5 步受控文件事务、冻结产物及其余第三批能力仍未完成；基础 Worker 可运行不能推导全部领域 API 已跨平台支持。用户已明确授权推送本分支并继续三平台 CI 验收，授权不扩大到默认分支推送、版本发布或关闭尚未进入远端默认分支的 Issue。


平台 CI 暴露已有快照缓存的时间戳合并问题：Windows 连续写入等长内容时，修改时间可能保持相同。许可加载不能使用这一缓存，因此 Host 与 Worker 的实际加载点强制重读字节；普通已加载 API 的授权检查保留原有快速路径。同时在任何缓存命中前先校验批准摘要，避免新 capture 的摘要与宿主许可不符却因元数据缓存提前返回。增加保持大小及时间不变的确定性回归，避免用 sleep 掩盖问题。


## 真实三平台基础门禁

[完整运行与原始报告](https://github.com/ECLTeam/EuoraCraft-Launcher/actions/runs/41280678365)，源码提交 `8cac0ba78046750cd4e2b356b5c713019e4ee559`，Python 3.11。三平台均成功从远端浅克隆检出全部子模块指针，并执行 Ruff 及格式检查。

| 平台 | 共用源码行为 | 原生边界 | 报告 |
| --- | --- | --- | --- |
| Windows x64 | 125 通过 | 5 通过 | [原始 JUnit](https://github.com/ECLTeam/EuoraCraft-Launcher/actions/runs/41280678365/artifacts/13072933133) |
| Linux x64 | 125 通过 | 11 通过 | [原始 JUnit](https://github.com/ECLTeam/EuoraCraft-Launcher/actions/runs/41280678365/artifacts/13073356291) |
| macOS ARM64 | 125 通过 | 11 通过 | [原始 JUnit](https://github.com/ECLTeam/EuoraCraft-Launcher/actions/runs/41280678365/artifacts/13072274337) |

必需用例均非空、无失败和跳过；7 个冻结用例或相关参数在源码门禁中明确排除。这里只证明表列处理器和源码基础路径，其他处理器、冻结产物、文件事务和 WebView 不在此结论内。

真实 runner 发现并修正了 POSIX 宿主进程组查询、macOS kqueue 的关闭方式和 Framework Python 启动器与实际镜像不一致的问题。macOS 选择宿主内核确认的真实解释器镜像，保持单一精确镜像认证，不增加候选路径回退。临时加载前诊断已移除，最终报告使用原始生产代码。

快照问题已登记为 [#98](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/98)，修复 `8137235`；在进入远端默认分支前保持打开。修复后本机重新执行主仓库完整后端回归，结果为 **2700 通过、51 跳过**；之后新增的 macOS 专属回归与源码入口回归均通过，并包含在上述三平台共用门禁中。此前一轮失败结果保留为调查记录，不将它改写成已通过。
