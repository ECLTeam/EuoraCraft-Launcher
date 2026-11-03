# 插件权限入口按需显示

## 目标与授权

用户要求：仅在插件需要用户授权时提供权限确认入口，修改后简单测试。
本次按该明确指令直接实施，不重复请求批准。登记为 [#110](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/110)，
相关的历史授权简化建议 #74 已关闭。

## 现状与选择

`Plugins.vue` 已对无权限声明的插件调用 `enableBasic`，该入口复核实际权限为空后启用；
但列表仅凭 `installation_id` 就显示“权限”按钮，因此仍能打开只有基础说明的弹窗。

选择按 `requested_permissions` 是否非空控制列表入口，复用既有启用和授权流程。
另一种做法是在空弹窗中增加说明，但不能消除多余确认，不采用。
已获授权的插件仍保留权限入口，便于调整和撤销；未获授权的插件仍需确认。
不改变后端权限、基础能力或授权存储。

## 实施步骤

1. 添加列表入口回归，覆盖空权限声明、待审阅权限和已授权权限。
2. 仅在具有安装标识且申请了额外权限时显示“权限”按钮。
3. 验证基础插件直接启用、需要权限的插件弹窗确认，以及已启用插件正常禁用。
4. 保留工作区其他未提交改动，仅提交本次改动。

## 验证范围

本次属于单页局部交互变更：执行受影响文件 Prettier、ESLint，前端类型检查，
插件页面、授权弹窗及既有基础启用 store 定向用例，再构建并实际启动隔离测试启动器。
检查 Classic/Folia 亮暗主题下的权限入口及启用行为，不运行无关全量测试。

## 验证结果

- 修复前：新增 7 个入口回归中，空权限插件的待审阅、已启用两项失败，另外 5 项通过。
- 修复后：`Plugins.permissions.test.ts`、`Plugins.test.ts`、`pluginStore.test.ts` 和
  `PluginPermissionsModal.test.ts` 共 42 项通过。
- 修改文件 Prettier、ESLint、`git diff --check` 通过；`pnpm build` 包含类型检查并通过。
  构建仍提示既有大分块警告。
- Windows 原生 WebView2：Classic/Folia × 亮暗 × 960×600、1200×800、1440×1000
  共 12 个状态的权限入口检查通过；基础示例直接启用/禁用、联网示例启用前显示授权确认通过。
  使用隔离数据目录；原始报告为
  `output/playwright/plugin-permission-entry/7a50d71d/result.json`，截图与报告同目录。
- 面板示例在真实 Worker 中仍因现有问题 [#101](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/101)
  启用失败；本次已补充现象，不修改该示例。它的空权限入口已正确隐藏；
  直接启用/禁用流程由同为无额外权限的 `worker_development` 示例验证。
- 按单页局部交互范围采用定向验证，不运行全量 Vitest 或无关后端测试。
- 仅本地提交及本地 main 集成；未推送，#110 保持打开，进入远端默认分支并验收后再关闭。
