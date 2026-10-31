# 外部规范治理实施与验收记录

日期：2026-10-31（Asia/Shanghai）。本轮按已批准的系统化治理方案实施；用户另行批准可访问性、首批依赖及复查新增开发依赖的定向修复。不包含远端推送、发布或仓库保护设置修改。

## 交付内容

- 固定 WCAG 2.2（2024-12-12）、ASVS 5.0.0 的适用条目，建立责任、证据及未覆盖边界。保留既有 Ruff、ESLint、Prettier、中文文档与 Conventional Commits。
- 主仓库增加 EditorConfig、独立报告采集与校验、Windows/Linux 运行及完整 Python 环境审计、前端生产及完整依赖审计、31 个真实 Vue 页面场景，以及有限期精确复核机制。
- 主仓库和前端独立 CI 使用只读权限、超时及 14 天报告产物；前端从固定主仓库提交读取同一治理工具和例外，避免复制与浮动引用。每周上海时间周五 03:00 重扫。
- 修复启动器设置开关、输入控件、进度条、标题徽章和弹窗的可访问名称或角色；同步六语言。修复已检出的文字对比度，沿用主题语义颜色，并按实际背景为主按钮选择黑色或白色正文。
- 修复调试工具已经缩小时仍拦截 Esc，导致焦点进入缩小控件后任务弹窗不能退出的问题；保留浮动调试窗口原有 Esc 收起行为。回归在修复前失败、修复后通过。
- 修复 Ruff CI 的过期 `--format github` 参数，改用实际验证通过的 `--output-format github`，并增加执行真实命令的回归。

主仓库实施期间与本地 `main` 的插件权限简化提交 `9c4c94c` 合并；前端基于 `806c4c0`，保留其授权流程及测试，扫描输入使用当前业务权限和资源契约。Core 与 Florolding 未修改。

## 依赖修复和开发工具兼容性

| 原命中 | 最终处置 |
| --- | --- |
| DOMPurify 3.4.16 | 更新下限和锁定版本至 3.4.17 |
| brace-expansion 5.0.9 | 对该精确版本覆盖到 5.0.12 |
| brace-expansion 1.1.18 | 初次更新至 1.1.21；移除其唯一 minimatch 3 调用链后，清理整条依赖及失效覆盖 |
| undici 8.11.2 | 对该精确版本覆盖到 8.12.2 |
| minimatch 3.1.5、10.2.6 | 移除旧导入检查插件的 3.x 调用链；精确覆盖 10.2.6 至 11.0.1 |
| glob 13.0.6 | 精确覆盖至 14.0.2；其依赖解析到 minimatch 11.0.3 |

新增公告见 [minimatch](https://github.com/advisories/GHSA-g3hv-75wg-m249)、[glob](https://github.com/advisories/GHSA-3g3m-2fjr-6pw4)。只更新审计命中的开发链，不把版本公告直接等同于产品可被利用。

旧 `eslint-plugin-import` 采用 minimatch 3 的函数导出，无法直接覆盖到新版。改用 `eslint-plugin-import-x` 4.17.1 的公开 `rules/order` 子路径，只注册本项目原来采用的排序规则，同步规则和 resolver 命名空间，保留全部分组、别名和字母顺序选项。新插件的其他规则仍引用 minimatch 11 移除的 `makeRe`；不导入未采用规则，不添加兼容实现或修改第三方源码。以后新增导入规则须验证其接口。

resolver 的旧插件可选 peer 定向移除，防止自动安装重新引入 minimatch 3；其余可选依赖设置不变。新增真实 ESLint 正反例验证合法分组通过、倒置顺序失败。上游原生支持修复版本后，应清理相应精确覆盖。

## 验证范围与证据

本轮涉及公共主题、依赖、测试设施及 CI，按 `AGENTS.md` 第 6 节执行全量检查。后续仅工具单点变化使用已确认的定向回归，未重复不受影响的全量测试。

| 范围 | 实际结果与证据 |
| --- | --- |
| 主仓库静态质量 | Ruff 全仓检查、ECL/tests 格式检查通过；文档链接及 `git diff --check` 通过 |
| 主仓库全量行为 | 合并后 2579 通过、33 跳过；`backend-tests-merged.txt`。保留一条子进程非 UTF-8 stderr 的线程警告，不将跳过项算作通过 |
| 治理工具最终变更 | 治理、工作流及架构测试 104 通过，包含损坏/空报告、异常返回码、覆盖缺失、过期/失效例外、确认违规不能被复核放行、结构定位及原始证据保留 |
| Core | Ruff 通过、41 项测试通过；`core-tests.txt`。提交未变化，复用已通过结果 |
| 前端最终质量 | 格式、ESLint、类型检查通过；183 个测试文件、1104 项通过、1 todo；构建通过。用两个 worker 执行完整集合，与 `pnpm check` 的检查项一致。报告为 `frontend-vitest-final.txt`、`frontend-build-final.txt` |
| Windows Python 依赖 | 干净运行环境 29 个第三方包、完整开发/构建环境 104 个第三方包均无公告命中；对应 `python-windows-*.json` |
| 前端依赖 | 生产 285 项、完整 814 项，均无公告命中并经统一门禁校验；`frontend-production.json`、`frontend-all.json` |
| 浏览器可访问性 | 31 场景，四种主题、六语言最小窗口与德语放大窗口；无确认违规，736 个对比度未决节点逐项复核。独立启动服务重复扫描后精确门禁通过，详见[复核记录](accessibility-review-2026-10-31.md) |
| Windows 原生窗口 | 实际启动 Python/Tauri/WebView2，使用独立数据目录、禁用插件和构建后的前端。20 组记录：四主题 × 三种实测 WebView 尺寸、四组任务弹窗 Tab/Shift+Tab/Esc 与焦点恢复、四组自定义背景/60% 卡片透明度/12px 模糊；均通过。`native/results.json` 与截图 |

报告与截图位于本地 `build/governance/`，不进入产品或用户数据。原生验收通过 CDP 向实际 WebView2 发送键盘操作，并枚举当前测试进程的窗口测量尺寸；没有使用 jsdom 代替布局，也未宣称完成真人屏幕阅读器验收。首批页面的共用弹窗、主题与表单交互按代表性入口验证，不扩展为全站巡检。

本机默认并行 Vitest 曾因内存不足退出，最终使用两个 worker 完整执行同一测试集合，不减少用例，不修改远端 CI 的既有质量检查。构建保留既有大分块提示；安装保留 Vue ESLint parser 的既有 peer 提示，未扩大到无关工具升级。

## 持续维护与未验证范围

人工复核仅适用于精确节点，2026-11-30 到期。691 个可见文本补充测量满足适用阈值，45 个当前视口不可见节点单独记录；不将不可见项算作可见通过。随机 ID 不改写原始证据，结构变化仍触发复核。没有依赖漏洞豁免。

本机使用 Windows Edge/WebView2；Playwright 捆绑 Chromium 下载因网络超时未完成，因此未将其称为已验证。Linux 审计及 Linux Chromium 运行由新增 CI 提供，本轮本地没有对应系统，尚未实跑；远端 CI、分支 required checks、打包平台及完整辅助技术测试尚未验证。不宣称整体 WCAG AA、ASVS 等级或发布验收通过。

问题按用户影响归并为 [依赖 #75](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/75)、[可访问性 #76](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/76)、[Ruff CI #77](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/77)。本地交付后保持打开，只有远端默认分支和所有 gitlink 均可获取并通过验收后再关闭。

Git 交付顺序为治理来源提交、前端固定引用提交、主仓库最终 gitlink，再先前端后主仓库集成本地 `main`。本地集成不等于远端门禁已经生效；推送和发布需另行授权。

治理来源为 `9ea32b416b1822316a69d9bdc8b09a5bc64aac56`，前端交付为 `d5b9bdfd0a534796d6b20e22e79d63516440876e`；原批准方案提交为 `9e66021`。实施结论集中在本记录与规范映射，已完成方案从 `plans/` 移除。
