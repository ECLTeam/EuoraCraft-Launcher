# 手动构建与发布加固实施方案

状态：已实施，随本次提交交付。

日期：2026-11-08。

关联：[手动构建自定义版本与发布](manual-build-release-inputs-2026-11-08.md)（已完成并随本次提交移除）。

## 1. 目标

1. 勾选发布但未填写版本号时直接失败，不允许用源码常量 `0.0.1-alpha` 创建标签。
2. 拒绝不高于已发布最新版本的发布，避免更新检查永远拿不到的版本占用标签。
3. 在 Actions 运行页面输出本次构建的版本、类型与发布意图，便于发布前人工核对。
4. 清理本地调试残留，收口已完成的方案文档。

## 2. 现状问题

- `resolve_build` 的手动流程在 `version` 留空时回落到 `_source_metadata()`，读到的是 `ECL/foundation/version.py` 的 `0.0.1-alpha`；`publish=true` 时门禁照样通过，创建 `v0.0.1-alpha` 标签并上传 prerelease。标签按第 9 节不可删除，属不可逆误操作。
- `_validate_manual_publication` 只校验“同名标签未被使用”，不校验版本单调性。`ECL/services/updates.py` 的更新检查在候选里取最高版本，后发布的低版本既拿不到用户，也会留下无意义的标签。
- 手动运行只暴露输入框，解析结果只在后续作业体现，没有可供核对的摘要。
- 原方案文档的方法名与日期实现描述与代码不一致（`_apply_beta_release_date` 实为 `_with_beta_release_date`，且实现使用固定 `+08:00` 而不是 `ZoneInfo`）。

## 3. 改动点

### 3.1 `packaging/release_metadata.py`

- 新增 `ReleaseMetadata.type_rank`（类属性，`MappingProxyType`）：`alpha < beta < rc < release` 的比较顺序。
- 新增 `_version_order(full_version)`：把完整版本转成可比较的顺序键（核心版本三段、类型序、预发布序号、发布日期数字），语义与语义化版本一致。
- 新增 `_tag_version_order(tag_name)`：标签转顺序键，无法解析的项目外标签返回 `None`，不参与比较也不阻断发布。
- 新增 `_remote_tag_names(root_path)`：用一次 `git ls-remote --tags --refs origin 'refs/tags/v*'` 取全部远端标签，替代原先只判断单个标签的查询。
- 新增 `_reject_version_regression(metadata, published_tags)`：本次版本不高于已发布最高版本时抛“版本必须高于已发布的最新版本”。
- `_validate_manual_publication` 增加远端标签列表读取与回退校验，本地/远端同名标签检查保持不变。
- `resolve_build` 在 `publish=true` 且 `version` 为空时抛“手动发布必须填写版本号，留空只会沿用源码版本”。
- 新增 `_release_summary(outputs)` 与 `_write_job_summary(lines)`：写入 `GITHUB_STEP_SUMMARY`，无该环境变量时静默跳过，写入失败降级为 stderr 提示，不阻断已通过校验的构建。

### 3.2 `.github/workflows/build.yml`

- `version` 输入说明补充“勾选发布时必填”。其余步骤与作业拓扑不变。

### 3.3 文档

- `AGENTS.md` 第 9 节：手动发布须填写版本号且高于已发布的最新版本。
- `docs/guides/release-process.md`：输入表与门禁顺序同步。

### 3.4 收尾

- 删除 `frontend/tests/browser/` 下 5 个本地调试产物（`debug_home.py`、`repro_124.py`、`home-debug.png`、两张 de-DE 衣柜截图）。
- `.gitignore` 末尾补换行。
- 移除已完成的原方案文档 `docs/plans/manual-build-release-inputs-2026-11-08.md`，结论记入本文件与提交信息。

## 4. 测试计划

`tests/test_release_metadata.py` 改用可配置的标签列表作为测试夹具，新增用例：

1. `publish=true` 且 `version` 为空 → 拒绝；
2. 版本低于已发布最高版本 → 拒绝；
3. 正式版已存在时再发布同基础版本的预发布 → 拒绝；
4. 版本高于已发布最高版本 → 通过；
5. 顺序键遵循 `alpha < beta < rc < 正式版`，同类按序号与日期递增；
6. 运行摘要包含版本、类型与发布意图。

## 5. 验收

1. `ruff check packaging tests`、`ruff format --check packaging tests`。
2. `python -m pytest tests/test_release_metadata.py --tb=short -q`。
3. 按第 6 节“构建打包与 CI 属于高风险变更”执行主仓库全量验证：`ruff check ECL tests`、`ruff format --check ECL tests`、`python -m pytest --tb=short -q`。

## 6. 风险

| 风险 | 处理 |
| --- | --- |
| 严格单调会阻止在更高预发布版本之后补发低版本正式版 | 与更新检查“只取最高版本”的现有行为一致；确需补发时先确认是否调整更新策略，不在此处放行 |
| 无法解析的历史标签导致比较异常 | 只比较可解析的项目标签，其余跳过且不影响发布 |
| 运行摘要写入失败影响发布 | 摘要仅用于人工核对，失败降级为 stderr 提示 |
