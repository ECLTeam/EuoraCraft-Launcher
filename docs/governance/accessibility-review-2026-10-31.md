# 可访问性未决项复核记录（2026-10-31）

本轮 Codex 已查看四种主题下六个场景的截图及六语言最小窗口截图，并核对每个未决节点的补充测量。此记录仅处理 axe 的 `color-contrast` 未决项，不豁免已确认违规，不表示完整 WCAG AA 合规。

## 方法与范围

使用实际 Vue 页面、本地 axe-core 4.11.1 和 Python Playwright 1.58.0。扫描上下文为页面或当前活动弹窗；已被弹窗遮挡的背景页面不作为当前弹窗的可操作内容。每个场景独立浏览器上下文，主题、语言和目标控件均有前置断言。

对可见文本，先记录文本行边界、样式、透明度和裁切范围；仅为测量暂时隐藏文字填充，截取相同布局的背景像素，再恢复样式。对整个可见文本矩形取保守的最低对比度，不依赖单个背景采样点，不用截图抗锯齿后的文字颜色冒充正文颜色。小字阈值 4.5，大字按适用条件使用 3；有失败即保留为违规，旧的 `manual-review` 不能放行。

引擎健康检查验证无名称按钮被拒绝、黑白文字通过、低对比文字被拒绝。补充测量只覆盖当前显示的普通文本和矩形背景；不取代复杂混合、滤镜、屏幕阅读器或其他辅助技术的专门验证。算法不作整体合规承诺。

所有可见测量项均满足对应阈值；不可见项按裁切/视口记录，没有将它们计入可见文字通过数量。窗口、长列表末项和模态键盘流程另由真实启动器验收记录说明。截图和原始节点证据保存在同次 `build/governance/` 报告与 CI 产物，不进入产品或用户文件。

## 逐场景结果

| 场景 | 可见测量项 | 最低对比度 | 当前视口不可见项 |
| --- | --- | --- | --- |
| `settings-classic-light-zh-CN-1200x800` | 31 | 6.025 | 2 |
| `tasks-classic-light-zh-CN-1200x800` | 30 | 5.865 | 0 |
| `account-login-classic-light-zh-CN-1200x800` | 4 | 5.671 | 4 |
| `instance-classic-light-zh-CN-1200x800` | 38 | 4.940 | 0 |
| `permissions-classic-light-zh-CN-1200x800` | 15 | 5.037 | 2 |
| `update-classic-light-zh-CN-1200x800` | 10 | 5.037 | 0 |
| `settings-classic-dark-zh-CN-1200x800` | 31 | 5.802 | 2 |
| `tasks-classic-dark-zh-CN-1200x800` | 30 | 7.234 | 0 |
| `account-login-classic-dark-zh-CN-1200x800` | 4 | 8.230 | 4 |
| `instance-classic-dark-zh-CN-1200x800` | 38 | 6.084 | 0 |
| `permissions-classic-dark-zh-CN-1200x800` | 15 | 5.037 | 2 |
| `update-classic-dark-zh-CN-1200x800` | 10 | 5.037 | 0 |
| `settings-folia-light-zh-CN-1200x800` | 31 | 5.821 | 2 |
| `tasks-folia-light-zh-CN-1200x800` | 30 | 5.832 | 0 |
| `account-login-folia-light-zh-CN-1200x800` | 4 | 4.760 | 4 |
| `instance-folia-light-zh-CN-1200x800` | 38 | 4.511 | 0 |
| `permissions-folia-light-zh-CN-1200x800` | 15 | 4.779 | 2 |
| `update-folia-light-zh-CN-1200x800` | 10 | 4.766 | 0 |
| `settings-folia-dark-zh-CN-1200x800` | 31 | 5.912 | 2 |
| `tasks-folia-dark-zh-CN-1200x800` | 30 | 7.390 | 0 |
| `account-login-folia-dark-zh-CN-1200x800` | 4 | 8.938 | 4 |
| `instance-folia-dark-zh-CN-1200x800` | 38 | 6.040 | 0 |
| `permissions-folia-dark-zh-CN-1200x800` | 15 | 5.037 | 2 |
| `update-folia-dark-zh-CN-1200x800` | 10 | 5.037 | 0 |
| `settings-classic-light-zh-CN-960x600` | 26 | 6.062 | 1 |
| `settings-classic-light-zh-TW-960x600` | 24 | 6.062 | 2 |
| `settings-classic-light-en-US-960x600` | 24 | 6.062 | 2 |
| `settings-classic-light-ja-JP-960x600` | 24 | 6.062 | 2 |
| `settings-classic-light-ru-RU-960x600` | 24 | 6.062 | 2 |
| `settings-classic-light-de-DE-960x600` | 24 | 6.062 | 2 |
| `settings-folia-dark-de-DE-1440x1000` | 33 | 5.847 | 2 |

## 有效期与变更

此次精确节点列表登记为 `manual-review`，2026-11-30 到期。未决节点增加、选择器改变、规则改变或补充测量失败均会使门禁失败，不自动刷新列表。节点减少产生的失效例外需要复核后清理。该日期是复核期限，不是问题修复期限或风险接受声明。

相关问题：[辅助技术控件与对比度问题 #76](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/76)。只有远端默认分支完成集成并通过验收后才按贡献指南关闭。

本次合并新版插件授权界面后，四个权限场景已重新查看截图及逐节点证据。保留原始 `target`，含动态 ID 的节点另记录经唯一性验证的 `governance_target` 结构定位；例外逐项匹配结构定位，不改变控件 ID 或可访问属性。结构变化仍需复核。
