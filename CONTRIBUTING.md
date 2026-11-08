# 参与贡献

欢迎报告问题、提出建议、完善文档或提交 Pull Request。提交内容可以使用中文或英文。

工程协作的强制要求以 [项目规范](AGENTS.md) 为准：变更流程见第 0 节，测试与验收见第 6 节，提交与本地交付见第 7 节，子模块与发布见第 8、9 节。本文件只说明参与入口和日常操作，与规范冲突时以规范为准。

## 可以先做什么

| 想做 | 入口 |
| --- | --- |
| 报告启动器异常 | [缺陷报告](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/new?template=01-bug-report.yml)，自动标记 `bug` |
| 提功能建议 | [功能建议](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/new?template=02-feature-request.yml)，自动标记 `enhancement` |
| 插件加载、授权或联机问题 | [插件与联机问题](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/new?template=03-plugin-and-connectivity.yml)，自动标记 `bug`、`plugin` |
| 文档写错或过期 | [文档问题](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/new?template=04-documentation.yml)，自动标记 `documentation` |
| 修代码、补文档 | 按下方「提交改动」流程发起 Pull Request |
| 使用问题、常见故障 | 先看[使用文档](https://docs.euora.cn)，仍未解决再按缺陷报告登记 |

提交表单会关闭普通用户的空白入口；维护者通过 API 或 CLI 建单时，请补齐表单要求的等价信息，必填校验不会自动执行。

## 提交 Issue

先查阅[使用文档](https://docs.euora.cn)，再搜索[已有 Issue](https://github.com/ECLTeam/EuoraCraft-Launcher/issues?q=is%3Aissue)，包括已关闭的问题。

- **一件事一条 Issue**：同一问题补充到原 Issue；同一次修复里的内部异常分支、防护补齐和实现细节不另拆单。只有具有独立用户影响、需要单独处理的问题才另建。
- **标题写具体行为**：例如「[问题] 切换实例后仍显示上一个实例的下载结果」，而不是「有 bug」。
- **写清触发条件与结果**：最短复现步骤、实际结果与预期结果；版本和系统按相关性填写，日志与截图分开提供。
- **版本信息**：发布版填写完整版本号；源码运行填写主仓库提交 SHA，涉及子模块时补充相应 SHA；无法获取时说明原因。
- **脱敏**：只提供相关日志片段，去除令牌、密码、账户凭据、房间码、私人路径和未经脱敏的个人信息；不要在公开 Issue 中提交敏感漏洞细节，本项目不提供未经确认的私密报告渠道。

Issue 只报告问题。实施方案、开发流水账、测试清单和本地交付报告放在方案文档、提交或 PR 中，关闭时只留简短结论和修复链接。

代码调查只保留定位所需的证据；未经确认的现象标记为待确认，不写成已确认的事实。

## 登记与处理问题

在已授权的调查、开发、评审或验收中发现已确认缺陷时，先查重，再创建或补充 Issue；暂不修复的问题也要登记并说明状态。登记问题不等于批准修复，实施仍遵循 [AGENTS.md](AGENTS.md) 第 0 节。

默认在主仓库登记并注明相关模块；子仓库已有同一问题时直接关联，避免重复建单。

在用户已授权的问题处理范围内，创建、补充和按条件关闭关联 Issue 属于必要协作操作，无需逐条重复请求批准。范围外的问题修改、向他人发送通知、代码推送和版本发布不在此授权内。网络或权限导致无法登记时保留待登记内容并告知用户，不编造 Issue 编号。

处理步骤：

1. 简要报告问题，按项目规范确认方案后实施修复。
2. 在提交正文或 PR 中写 `Refs #编号`；跨仓库写完整的 `Refs ECLTeam/EuoraCraft-Launcher#编号`。
3. 按第 6 节完成回归与验收，并在提交或 PR 中记录实际执行的检查；行为修复补充修复前失败、修复后通过的回归用例，纯视觉修复默认在真实浏览器查看效果。
4. 提交并集成到本地 `main`，保留 Issue 打开；取得本次推送授权后，按第 8 节完成子模块和主仓库的远端交付。

## 关闭与重新打开

以「已修复」关闭前，修复必须进入远端默认分支，并完成第 6 节要求的测试与验收。涉及子模块时，子仓库修复提交必须能从远端取得，主仓库远端默认分支必须引用修复指针，受影响的集成验证也必须通过。

- 只完成本地提交、子仓库合入或部分修复时，主仓库 Issue 保持打开。
- `Fixes #编号`、`Closes #编号` 等关键字只用于合入远端默认分支后即可满足全部关闭条件的最终交付；子模块提交、分阶段修复和合入后仍需验收的 PR 使用普通关联。
- 用户明确要求以已验收的远端功能分支结单时按该指令关闭，并在结论中注明分支或修复提交；这不授权默认分支推送或版本发布。
- 手动关闭只留一句结果与修复提交或 PR 链接，验证明细保留在对应提交或 PR。

重复、无法复现或决定不处理时，简要说明原因并选择对应关闭原因，不描述为已修复：重复问题链接原 Issue；无法复现说明环境与缺失条件；不接受建议说明理由。后续确认同一问题仍存在时重新打开原 Issue 并补充证据；原因独立的问题另建 Issue 并关联。

Issue 关闭与版本发布分别报告：修复进入默认分支，不代表用户已经可以下载到包含修复的版本。

## 提交改动

1. 检出仓库并初始化子模块：`git clone --recurse-submodules`，或对已有仓库执行 `git submodule update --init --recursive`。
2. 环境要求：Python 3.11+、Node.js 22、pnpm 10。后端用 `pip install -e ".[dev]"`，前端用 `pnpm install --frozen-lockfile`。
3. 改动前先按 [AGENTS.md](AGENTS.md) 第 0 节确认需求与方案；未经确认不要直接提交实现。
4. 提交信息使用中文 Conventional Commits（`feat`、`fix`、`perf`、`docs`、`style`、`refactor`、`test`、`chore`），例如 `fix: 修复更新弹窗重复提示`；一次提交只包含同一功能的相关文件。
5. PR 按模板说明关联 Issue、具体改动、实际执行的检查和待完成事项。纯文档变更没有关联 Issue 时说明原因即可。Issue 或 PR 中的讨论不替代实施批准。

## 检查会在什么时候运行

自动化检查按改动范围触发，无关改动不会点亮全部分支。主分支推送始终比 PR 更严格：发布门禁需要 `main` 上最近一次 `Backend Tests` 与 `Frontend Checks` 成功，因此这两个工作流的推送运行不做路径过滤。

| 改动范围 | PR 上执行 | 推送到 `main` |
| --- | --- | --- |
| `ECL/`、`tests/`、`packaging/`、`native/`、`tools/`、`main.py`、`pyproject.toml` | Backend Tests、Ruff Lint | 全量 |
| `frontend` 子模块指针 | Frontend Checks | 全量 |
| `ECL/plugins/`、`ECL/plugin_api/` 等插件运行时路径 | Plugin Worker Platforms（Windows / Linux / macOS） | 按路径 |
| 依赖清单：`pyproject.toml`、`frontend/package.json`、`frontend/pnpm-lock.yaml` | Dependency Review（依赖审计） | 每周治理任务 |
| `docs/governance/`、`ECL/tooling/governance.py`、扫描脚本 | Engineering Governance（含 31 个场景的可访问性扫描） | 每次推送 + 每周四定时 |
| 仅文档、模板、图片 | 无自动检查，按第 6 节自查格式、结构与链接 | — |

几点说明：

- **依赖更新**走单独工作流。Renovate 的 PR 只有清单变动时才跑审计，不会因为页面扫描与本次升级无关而变红。
- **可访问性扫描**在 PR 上只跟随治理相关改动；日常界面改动由 `main` 推送与每周定时发现，避免每次 UI 调整都被整套扫描阻塞。
- 同一分支连续推送会取消上一次未完成的运行，只保留最新结果。
- 仓库当前未启用分支保护，红灯不会自动阻止合并；合并前仍需修复失败项，或按第 6 节说明未运行全量检查的原因。

## 检查与验收

检查范围按 [AGENTS.md](AGENTS.md) 第 6.1 节确定，命令见第 6.2 节。文档、模板和纯样式整理只需检查格式、结构与链接；改动后端、前端交互、构建打包或 CI 时，按对应范围执行定向或全量检查。

依赖公告命中、可访问性和安全检查按[工程治理](docs/governance/README.md)记录实际范围；版本命中不直接等同于已证实可利用。PR 或提交说明自动检查、人工复核和有限期例外的依据，不自动用现存问题豁免新增门禁。

未完成的验收、远端交付和发布如实保留在待办中，不把本地提交描述为已经交付。
