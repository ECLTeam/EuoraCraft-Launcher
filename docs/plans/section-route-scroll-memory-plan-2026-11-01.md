# 分区子页面与滚动位置记忆实施方案

日期：2026-11-01  
状态：已实施

## 目标与现状

「设置」「更多」这类页面在左侧有子页面导航（启动器 / 外观 / 游戏 / Java 管理 / 关于；联机 / 插件 / 工具）。当前行为：

- 侧边栏「设置」始终跳到 `/settings/launcher`（路由上写死 `redirect`），「更多」跳到语言相关的默认子页面，用户上次停留的子页面不会被记住。
- 子页面内容区滚动容器是 `SectionLayout` 内的 `.section-layout__viewport`，离开分区再回来会从顶部开始。

需求：记住用户上次选择的子页面与各子页面的滚动位置，从其他路由回来后仍停在原来的子页面和位置。

## 选定方案

1. **新增运行期记忆模块** `frontend/src/app/runtime/sectionMemory.ts`，保存两张表：分区根路径 → 最近子页面、子页面路径 → 滚动位置。只在内存中保留，不写配置：浏览行为不应污染用户配置，重启后回到默认子页面。
2. **路由记录与入口解析**：`router.afterEach` 对嵌套路由（`to.matched.length >= 2`）按 `to.matched[0].path` 记录最近子页面；`/settings`、`/more` 的 `redirect` 改为调用 `settingsEntryPath()` / `moreEntryPath()`，优先返回记忆中的子页面，未记录时退回原有默认值（更多页仍遵循语言与联机可用性规则）。
3. **滚动位置保存与恢复**：在 `SectionLayout` 中处理，设置页与更多页共用。
   - 子页面切换时，在路由变化但 DOM 尚未替换的时机读取旧位置保存，再在 `nextTick` 后恢复新位置。
   - 离开分区（组件卸载）时保存当前子页面位置；由于路由对象会先于组件卸载更新，额外记录 `activePath`，避免把位置存到新路径上。
   - 子页面切换使用 out-in 过渡，内容高度稳定前滚动会被钳制，因此恢复在下一帧与过渡结束后（450ms）各再对齐一次，并在切换或卸载时取消待执行任务。

## 修改范围

- `frontend/src/app/runtime/sectionMemory.ts`（新增）+ `sectionMemory.test.ts`。
- `frontend/src/router/index.ts`：`afterEach` 记录、`moreEntryPath()` / `settingsEntryPath()` 与重定向；`router/index.test.ts` 补充用例。
- `frontend/src/components/layout/SectionLayout.vue`：滚动容器 `ref`、保存 / 恢复逻辑与资源清理；新增 `SectionLayout.test.ts`。

## 验证

- `eslint`、`prettier --check`、`vue-tsc` 通过；`vitest run` 全量 195 文件 / 1195 用例通过。
- 用例覆盖：未记录时的默认子页面、按分区独立记忆、滚动位置取整与负数归零、重置清空；子页面之间切换各自记住位置、离开分区再回来恢复；分区入口解析优先返回记忆值。
- 演示模式实测：在「关于」滚动到 520px → 切到游戏页 → 点击侧边栏「设置」，回到 `/settings/about` 且滚动位置为 520px。

## 风险与边界

- 记忆仅限本次运行，重启后回到默认子页面与顶部；如需跨重启保留，需要另加配置项并评估配置语义。
- 记忆以路径为键，不区分查询参数；子页面若依赖查询参数表示不同内容，回到该路径时不还原参数。
- 子页面内容异步加载较慢（例如贡献者图片）时，过渡结束后的对齐仍以当时的滚动高度为准，超出部分会被浏览器钳制。
