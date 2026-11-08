# 可访问性定位与扫描一致性修复实施方案

状态：已实施，等待提交。

日期：2026-11-08。

关联：Issue [#100](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/100)，复核记录 [accessibility-review-2026-11-08](../governance/accessibility-review-2026-11-08.md)。

## 1. 目标

1. 让可访问性例外与当前扫描结果重新对齐，`Engineering Governance` 在 `main` 恢复通过。
2. 降低例外随界面改版失效的频率，避免每次改动都要重绑数百个定位。
3. 消除 Windows 生成基线、Linux 执行门禁导致的场景节点数不一致。

## 2. 现状与根因

- 门禁失败信息为“当前范围存在无匹配的失效例外”：`exceptions.json` 747 个定位中 142 个已失配、130 个当前位置无批准。
- 报告本身没有确认违规：735 个 `color-contrast` 待复核位置中 689 个像素测量通过、46 个当前视口不可见、0 个失败。
- 定位失效的直接原因：axe 原始定位包含 `data-v-*` 构建期哈希与整链 `nth-child`，任何样式或结构改动都会改变。
- 更深一层的原因：`InstanceDetailOverviewTab.vue` 使用 `v-if="isWindows"`（按 UA 判断）渲染快捷方式区块。CI 在 Linux 只扫描到 735 个位置，Windows 本机扫描到 747 个，差集正好是该区块，因此无论怎样重绑都无法在两端同时成立。

## 3. 改动点

### 3.1 `frontend/tests/browser/test_accessibility.py`

- 新增 `windows_user_agent`，`prepare_page` 把每个上下文固定为该 UA，使两端渲染同一份 DOM，Windows 专属区块也进入 CI 覆盖。
- 重写 `stabilize_targets`：优先 `data-testid`、`data-theme-component`、`aria-label`、`title`、`name` 与稳定类名；其次以最近的唯一祖先为锚点拼接相对路径；最后退回完整结构链。定位不含 `data-v-*`，逐节点验证唯一性，并在同一场景内去重。
- 每个节点记录 `locator_kind`（`semantic` / `semantic-anchored` / `structural`），供复核判断定位质量。
- 引擎自检增加断言：语义锚点定位不携带 `data-v-*`，随机 ID 变化不改变定位。

### 3.2 `docs/governance/exceptions.json`

- `frontend-accessibility` 31 条例外的定位按新报告重绑为 747 个，规则、场景、仓库、原因、关联问题、责任人、风险分类保持不变。
- `approval` 指向本轮复核记录，`approved_on` 改为 2026-11-08，`expires_on` 改为 2026-12-08（保持 30 天上限）。

### 3.3 文档

- 新增 `docs/governance/accessibility-review-2026-11-08.md`：根因、逐场景数据、实际查看范围与遗留限制。
- `docs/governance/accessibility.md`：补充固定 UA 与语义锚点定位约定，更新当前证据。
- `frontend/tests/browser/README.md`：同步定位策略与自检说明。

## 4. 测试与验收

1. 本地以 Playwright 1.58.0、Chromium 145.0.7632.6、axe-core 4.11.1 复跑 31 个场景，与历史 CI 报告逐项比对：735 个共有节点的测量状态全部一致，差异仅为 `isWindows` 区块。
2. 新定位报告节点数 747，与定位风格改造前逐场景完全一致，说明只更换定位、没有丢失或新增覆盖。
3. `python ECL/tooling/governance.py check --scope frontend-accessibility --report <新报告> --exceptions docs/governance/exceptions.json` 通过，检查数量 31。
4. 前端 `pnpm exec prettier --check` 覆盖改动的 `frontend/tests/browser/README.md`；主仓库按文档类改动检查 Markdown 结构与 `git diff --check`。

## 5. 风险

| 风险 | 处理 |
| --- | --- |
| 固定 Windows UA 使非 Windows 专属界面不被扫描 | 平台分支仅此一处，固定后扫描的是超集；后续新增平台分支时须同步纳入场景 |
| Linux 与 Windows 字体渲染差异影响像素测量 | 节点集合由 DOM 决定，不受字体影响；若 CI 出现新的 `failed` 发现，按实际报告逐项处理 |
| 语义锚点在组件内部仍可能因重排失效 | 定位已去掉构建期哈希，剩余 105 个结构链兜底位置随界面重排仍需重绑，属已知残余成本 |
| 例外重绑等于放行新风险 | 本轮 0 个确认违规，701 个位置像素测量通过、46 个当前视口不可见、0 个失败；复核记录写明未逐张人工查看的场景，推送后核对 Linux 结果 |
