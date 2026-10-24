# 实例列表启动时同步选中项实施方案

日期：2026-10-24（Asia/Shanghai）。

状态：用户已选择方案 A 并明确确认「开始实施」；已完成局部修复、自动化验证、真实启动器验收及原工作区本地 `main` 交付，未推送远端。详情见第 7 节。

## 1. 问题与已选方案

当前选中实例 A 时，直接点击实例 B 的行内启动按钮，会启动 B，但没有同步全局选中项。游戏页的快捷启动因此仍可能显示 A，返回实例列表时高亮也与刚启动的实例不一致。

调查基线：主仓库 `5bc60060831071419f81f885fc12a20477befcc1`，前端 `d9210fd5f792038ae5b282145f36c21603e4fafd`。

用户已选择 A：在实例管理页发起启动时，同步选中该实例及所属游戏目录。B 所涉及的启动流程统一、异步目标固定和最近启动记录整理不纳入本次实施。

## 2. 代码证据与调用范围

- `frontend/src/components/instances/InstalledInstanceList.vue` 的卡片与表格行内启动按钮使用 `@click.stop`，只发出 `launch`，因此不会触发行的 `selectVersion`。
- `frontend/src/views/instances/ManageTab.vue` 的 `handleLaunch` 收到实例后直接进入启动流程并导航到游戏页，没有调用 `instanceStore.selectVersion`。
- `frontend/src/views/Game.vue` 通过 `useInstanceManager` 读取同一个实例 store 的 `selectedVersion`、`currentGamePath`，并传给快捷启动组件。
- `instanceStore.selectVersion` 已负责更新选中状态，并沿用现有全局活动目录和目录选中版本的保存机制；本次直接调用，不增加第二份状态、配置字段或持久化入口。
- 管理页的 `action === 'launch'` 处理分支、详情启动同样进入 `handleLaunch`，因此共享这一次选中同步。当前可见的更多菜单没有启动条目，自动化测试覆盖其 action 分支，真实界面验收使用列表、卡片及详情入口；没有新增菜单项。

## 3. 具体改动与行为

生产代码预期只修改 `frontend/src/views/instances/ManageTab.vue`：

1. 保留现有游戏目录检查与 `launchInFlight` 防重入判断。
2. 在接受本次启动请求后、导航游戏页之前，调用 `instanceStore.selectVersion`，传入本次实例的 `versionId` 和当前管理目录的路径，与当前启动请求使用的目录一致。
3. 保留行内按钮的事件阻止，避免额外触发行点击；选中同步由接收启动请求的页面完成。
4. 保持已有启动进度、后端调用和最近启动成功记录规则。启动失败或取消时保留刚选中的实例，方便用户从游戏页重试；不会把失败或取消新增为成功记录。
5. 重复请求被现有执行锁拒绝时，不因该次点击切换选中项。

预期效果：选中 A 后直接启动 B，在跳转到游戏页前全局选中项已变为 B；游戏页当前快捷启动显示 B，返回管理列表时 B 高亮。使用现有保存机制，重新加载后仍按已保存的活动实例恢复。

本次所说的快捷启动同步是当前实例及高亮同步。「最近启动」列表仍按既有成功条件记录；不会把仅点击启动或启动失败解释为最近成功启动。

## 4. 实施步骤与测试计划

1. 复查两仓库工作区，记录并保护既有无关修改。
2. 在 `ManageTab.test.ts` 中补充回归：初始选中 A，发出 B 的列表启动事件，断言导航及启动请求开始前已选中 B 和对应目录；先验证该用例在修复前失败。
3. 在 `handleLaunch` 增加选中同步，再验证回归通过。
4. 补充或复用直接调用方测试，覆盖不同目录中同名实例的目录参数、重复请求不改变选中项，以及共享菜单/详情入口继续进入同一启动处理。
5. 检查 `InstalledInstanceList.test.ts` 和 `GameLaunchBar.test.ts` 对行内按钮、当前实例展示与高亮的既有行为，不修改组件契约。
6. 按局部前端交互修复执行改动文件 Prettier、ESLint、`pnpm typecheck` 和相关 Vitest（`ManageTab.test.ts`、`InstalledInstanceList.test.ts`、`GameLaunchBar.test.ts`，按实际影响补充现有实例选择测试）。不改变共享 store、配置结构、IPC 或后端行为，不运行无关后端全量测试或前端全量 Vitest。
7. 执行 `pnpm build` 后，在真实 Windows 启动器用临时测试目录验证：未先选中 B 就点击启动；卡片/表格及共享入口的选中同步；游戏页当前实例与返回列表高亮；失败后可重试。检查 Classic/Folia 亮暗主题，以及默认、最小、放大窗口中的相关标识可见性。真实启动成功条件不可用时明确记录，不能把前置校验失败算作成功启动。
8. 文档及代码检查使用 `git diff --check`；按实际结果更新本文，分别报告静态检查、自动化测试和真实界面验收。

不新增控件或文案，预期不需要修改六语言文件、样式或后端。

## 5. Issue 与未纳入范围的问题

此前已搜索 `repo:ECLTeam/EuoraCraft-Launcher is:issue 快捷启动`，未找到重复 Issue。尝试创建 `[问题] 从实例列表启动后游戏页未同步选中的实例`，GitHub 连接器返回 `403 Resource not accessible by integration`，没有创建成功。

待登记正文已包含 Windows 环境、上述完整提交、复现步骤、预期与实际结果及代码调用链。当前状态为已本地修复、待登记；连接器权限恢复后先查重再补登，不填写虚构编号。未推送前不以已修复关闭远端 Issue。

同一调查还发现管理页在等待事件监听后读取可变化的 `currentPath`，最近启动也读取当时目录。此问题保留待登记内容，但用户选择 A 后不在本次修改它。后续处理须另行确认范围，不能把本次局部选中同步宣称为启动链路全面整理。

## 6. 本地交付与保护范围

本文先作为独立 `docs` 提交到原工作区本地 `main`。用户审阅并明确「开始实施」后才修改生产代码与测试。

功能通过适用检查和真实验收后，提交前端中文 Conventional Commit，先确认前端原本地 `main`，再更新主仓库前端指针及验收记录，完成主仓库原本地 `main` 交付。当前请求没有远端推送或发布授权。

必须保留且不纳入本次提交的既有修改：

- 主仓库 `packaging/arch/euoracraft-launcher.desktop`，当前 SHA256 为 `78de642a34453783fb49996cf8296b617e2739db49189d1163d681b14c039e5b`。
- 前端 `src/i18n/locales/zh-CN.json` 的 `settings.modSourceDetails` 既有一行说明修改。

## 7. 当前阶段记录

已按 A 方案在 `handleLaunch` 的目录检查、防重入判断之后，导航游戏页之前，增加一处 `instanceStore.selectVersion(version.versionId, currentPath.value.path)` 调用。生产代码仅增加这一行，沿用现有状态和保存机制。

回归测试先在修复前运行：新增 4 项失败，5 项原有用例通过，失败准确反映导航时仍是旧实例/旧目录或没有同步选中。修复后，列表、菜单、详情启动以及重复请求保护的新增用例通过。

本次按 AGENTS 第 6 节「影响范围明确的前端局部交互修复」执行定向检查：

- 改动 Vue/测试文件的 Prettier、ESLint 与 `pnpm typecheck`：通过。
- `pnpm exec vitest run src/views/instances/ManageTab.test.ts src/components/instances/InstalledInstanceList.test.ts src/components/game/GameLaunchBar.test.ts src/features/instances/stores/instanceStore.selection.test.ts --reporter=dot --silent`：4 个文件、55 项通过。
- `pnpm build`：通过；存在已有的大分包体积提示，没有构建错误。
- 不修改共享 store、配置、IPC、样式或后端，因此没有运行全量 Vitest 或无关后端检查。

真实 Windows Tauri/WebView2 使用临时配置、测试实例及独立账户主目录；未添加账户，后端实际启动请求返回 `ACCOUNT_REQUIRED`，用于验证失败路径，不能作为游戏成功启动的证据。

真实验收结果：

- 通过本次测试进程树定位主窗口，稳定核对原生客户区和 WebView 尺寸：960×600、1100×720、1400×900。
- 最小窗口下覆盖 Classic/Folia × 亮/暗；Folia 使用背景、80% 卡片透明度与 12px 模糊。默认窗口使用 Classic 亮色，放大窗口使用 Folia 亮色，相关实例名称完整可见。
- 列表中先选中 `demo-vanilla`，直接点击 `demo-fabric` 的启动按钮；实际后端请求失败后，游戏页当前快捷启动显示 `demo-fabric`，返回列表时该实例高亮，目录配置中的 `activeVersion` 同样为它。
- 卡片启动按钮、详情总览启动按钮完成相同流程并通过；菜单 action 分支由组件测试覆盖，没有将不存在的可见菜单项宣称为实机通过。
- 整页重新加载后仍选中 `demo-fabric`；失败流程后最近成功启动列表仍为 0 条。
- 未修改游戏启动异步目标、进度或最近记录规则，未执行真实游戏成功启动；本次结论限于 A 方案的选中同步及上述失败路径。

本地交付：前端原本地 `main` 已提交 `56edc7a8d67cf5b663b8ae6a41d72c0c90d2ad70`，主仓库更新对应 gitlink 和本文。获取远端后，提交前主仓库领先 5 个提交、前端领先 2 个提交，均未落后或分叉，无需额外合并。原有中文说明和 desktop 文件修改保留且未提交；没有远端推送或发布。
