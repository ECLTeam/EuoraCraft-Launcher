# 手动构建自定义版本与发布实施方案

状态：已实施，等待全量后端测试与提交。

日期：2026-11-08。

## 1. 目标

1. 手动运行 `Build` 时可填写版本号与版本类型，构建产物和文件名使用该版本，而不是源码常量 `0.0.1-alpha`。
2. 手动运行可选择是否直接创建版本标签并发布 GitHub Release。
3. 手动发布复用标签发布的全部门禁：目标提交是远端 `main` 当前提交、该提交最近一次 `Backend Tests` 与 `Frontend Checks` 成功、Beta 日期与序号合规、标签未被使用。
4. 不填写任何输入时保持现状：使用源码版本、只上传 Actions 产物、不创建 Release。
5. 顺手把 `design_base` 改为可选，留空时按现有规则与上一提交比较。

## 2. 现状

- `build.yml` 的 `workflow_dispatch` 只有 `design_base` 一个必填输入，版本固定取 `ECL/foundation/version.py`。
- `packaging/release_metadata.py` 的 `resolve_build` 只在 `push` + `tag` 时返回 `publish=true`；手动运行一律 `publish=false`。
- `build.yml` 中 `Upload to Release` 与 `publish-arch` 都以 `publish == 'true'` 为条件。
- 发布门禁 `_validate_publication` 只处理“标签已存在”的场景：校验标签指向远端 `main`、Beta 标签格式与注释类型、两项主分支检查成功。

## 3. 与项目规范的关系

AGENTS.md 第 9 节现规定“手动构建只提供 Actions 产物，不创建或更新 Release”。本方案按已确认的选择调整该条：手动发布必须通过与标签发布相同的门禁，并在发布文档写明“标签创建后构建失败不删除标签、改用新版本号”的失败处理。

## 4. 新增输入

| 输入 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `version` | string，可选 | 空 | 不含 `v` 的完整版本，例如 `0.3.0`、`0.3.0-beta.1`；留空使用源码版本 |
| `version_type` | choice：`auto` / `alpha` / `beta` / `rc` / `release` | `auto` | 与 `version` 不一致时构建失败 |
| `publish` | boolean | `false` | 为真时创建标签并上传 Release；仅允许从 `main` 分支手动运行 |
| `design_base` | string，可选 | 空 | 前端设计检查基线提交 SHA，留空与上一提交比较 |

## 5. 版本解析与校验顺序

1. **事件判断**：`push` + `tag` 走现有标签流程；`workflow_dispatch` 走手动流程；其余拒绝。
2. **版本来源**：手动流程优先使用 `version` 输入；留空时读取源码版本常量。
3. **类型一致性**：`version_type` 非 `auto` 时必须与解析结果一致，否则报“版本号与版本类型不一致”。
4. **Beta 日期**：`beta` 且版本未带 `+YYYYMMDD` 时按 Asia/Shanghai 当天补全；已带日期时必须等于当天。
5. **发布门禁**（仅 `publish=true`，按顺序）：
   1. 事件来源是 `workflow_dispatch`、ref 类型为分支、分支名为 `main`；
   2. `HEAD` 等于 `origin/main`（先 `git fetch origin main`）；
   3. 该提交最近一次主分支推送的 `Backend Tests`、`Frontend Checks` 均成功（复用现有 `latest_main_run_succeeded`）；
   4. 远端与本地都不存在同名标签（`git ls-remote --tags origin` + `git rev-parse --verify`）。
6. **创建标签**：门禁通过后由 `validate-release-tag` 作业创建带注释标签并推送；使用 `GITHUB_TOKEN` 推送不会再次触发 `Build` 工作流。
7. **发布**：`publish=true` 时沿用现有 `softprops/action-gh-release` 上传各平台产物，`prerelease` 按版本类型计算。

## 6. 具体改动点

### 6.1 `.github/workflows/build.yml`

- `workflow_dispatch.inputs` 增加 `version`、`version_type`、`publish`；`design_base` 改为 `required: false`。
- `validate-release-tag` 作业 `permissions` 增加 `contents: write`（推送标签）。
- `Resolve version and validate publication` 步骤传入 `ECL_MANUAL_VERSION`、`ECL_MANUAL_VERSION_TYPE`、`ECL_MANUAL_PUBLISH` 环境变量。
- 新增步骤 `Create release tag`：仅 `publish == 'true'` 时执行，`git config` 使用 `github-actions[bot]`，创建并推送 `v{full_version}` 带注释标签。
- 现有上传与 Arch 发布条件不变。

### 6.2 `packaging/release_metadata.py`

- 新增 `ManualBuildInput`（`dataclass(frozen=True, slots=True)`）承载三个输入。
- 新增 `_manual_metadata(inputs, root_path)`：解析版本输入并校验类型一致性。
- 新增 `_apply_beta_release_date(metadata, today)`：Beta 日期补全与校验，`today` 由调用方传入便于测试。
- 新增 `_validate_manual_publication(metadata, repository, root_path)`：第 5 节门禁。
- 把现有“两项主分支检查”逻辑抽为 `_validate_main_checks(metadata, repository, root_path)`，标签与手动流程共用。
- `resolve_build` 按事件分派；输出字段保持不变，`publish` 由输入决定。

### 6.3 文档

- `AGENTS.md` 第 9 节：更新“手动构建”一句，改为“手动构建默认只产出 Actions 产物；显式选择发布时须通过与标签发布相同的门禁”。
- `docs/guides/release-process.md`：更新“日常检查与完整构建”表格与手动运行说明，补充输入含义、门禁顺序和失败处理。

### 6.4 测试

新增 `tests/test_release_metadata.py`，通过 monkeypatch `_run_command` 覆盖：

1. 手动无输入 → 源码版本、`publish=false`；
2. 手动自定义版本 → 产物字段正确；
3. `version_type` 与版本不一致 → 拒绝；
4. `publish=true` 但不在 `main` → 拒绝；
5. `publish=true` 但标签已存在 → 拒绝；
6. Beta 自动补日期、日期不等于当天 → 拒绝；
7. 门禁全部满足 → `publish=true`；
8. 标签推送事件行为不变。

## 7. 失败处理

- 标签在构建开始前创建；任一平台构建失败时按第 9 节“标签不可删除”处理：修复后使用新版本号（Beta 递增 `N`，正式版递增 `PATCH`）重新发布，不改动旧标签。
- 重复点击同一版本会在标签校验阶段直接失败，避免覆盖。
- 手动发布失败不会删除或移动已有 Release 产物。

## 8. 验收

1. `ruff check packaging tests`、`ruff format --check packaging tests`。
2. `python -m pytest tests/test_release_metadata.py --tb=short -q`。
3. 解析 `build.yml` 确认 YAML 合法，检查 `workflow_dispatch` 输入与作业权限。
4. 由于涉及 CI 与发布流程，提交前按第 6 节执行主仓库后端全量验证：`ruff check ECL tests`、`ruff format --check ECL tests`、`python -m pytest --tb=short -q`。

## 9. 风险

| 风险 | 处理 |
| --- | --- |
| 手动发布绕过“标签必须由人创建”的既有习惯 | 门禁与标签发布完全一致，并在文档标注手动发布入口 |
| 标签先建后构建失败 | 明确不删除标签，改用新版本号；与现有标签推送失败处理一致 |
| `publish=true` 时 `GITHUB_TOKEN` 权限不足 | `validate-release-tag` 作业显式声明 `contents: write` |
| 时区导致 Beta 日期不一致 | 统一使用 `zoneinfo.ZoneInfo("Asia/Shanghai")`，并在测试中固定日期 |
