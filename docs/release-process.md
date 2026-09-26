# Beta 发布流程

本流程约束 EuoraCraft Launcher 的 beta 发行。发行由主仓库 Git 标签触发，GitHub Actions 负责构建并创建 GitHub prerelease；前端仓库只作为子模块先行推送，不单独创建与主仓库发行对应的标签。

## 标签格式与版本推进

beta 标签必须严格使用：

```text
vMAJOR.MINOR.PATCH-beta.N+YYYYMMDD
```

例如：`v0.1.0-beta.2+20260919`。

- `MAJOR.MINOR.PATCH` 遵循语义化版本；需要不兼容变更时升级 MAJOR，新增兼容功能时升级 MINOR，缺陷修复时升级 PATCH。
- `N` 是同一基础版本的 beta 序号，从 `1` 开始严格递增；基础版本变化后重置为 `1`。
- `YYYYMMDD` 使用 Asia/Shanghai 的实际创建日期，必须是有效日历日期。
- beta 标签必须是带注释标签。标签、构建元数据和上传产物名称均不可修改或复用。

CI 会拒绝不符合格式、日期无效、轻量标签、未指向远端 `main` 当前提交，或其对应提交尚未通过主分支后端测试与构建工作流的 beta 标签。主分支构建会执行 `pnpm check` 后再构建前端，因此标签门禁同时要求前端质量检查成功。

## 发布前置条件

1. 所有待发布功能均已提交；工作区必须干净，不得携带构建产物、临时文件或无关修改。
2. 按 `AGENTS.md` 第 5 节检查主仓库记录的**全部**子模块指针；先推送所有尚未到达各自远端分支的子模块提交，再提交并推送主仓库。即使本次未修改某个子模块，也不得跳过其指针检查。
3. 在 GitHub 上确认该主仓库提交的 CI 全部通过，包括后端测试、前端检查和构建矩阵。
4. 确认该提交就是远端 `main` 的当前提交；不要从历史提交、分支提交或本地未推送提交创建 beta 标签。
5. 确认下一枚 beta 标签未在本地或远端存在。

## 发布命令

以下命令中的版本仅为示例，发布时替换为下一枚合法 beta 标签：

```bash
git -C ECL/game push origin main
git -C ECL/services/florolding push origin main
git -C frontend push origin main
git push origin main
git status --short
git log -1 --oneline origin/main
git tag -a v0.1.0-beta.3+20260919 -m "v0.1.0-beta.3+20260919"
git push origin v0.1.0-beta.3+20260919
```

创建标签前，`git status --short` 必须无输出，且 `git log -1` 的提交必须与 `origin/main` 一致。不要用 `--force`、`--tags` 或 `--follow-tags` 批量推送标签。

## 发布完成判定

推送标签后，GitHub Actions 会以标签版本注入运行时信息，构建各平台产物并创建标记为 prerelease 的 GitHub Release。发布者必须等待构建矩阵与 Arch 发布任务完成，并在 Release 页面确认预期平台产物均可下载。

发布完成后至少记录：标签、目标提交、前端子模块提交、构建工作流链接、已上传产物与已知限制。

## 失败与回滚

- 标签尚未推送时：可删除本地标签并重新创建。
- 远端标签已推送但构建环境临时失败：不改动标签、不修改源码，直接在 GitHub Actions 重跑失败任务。
- 远端标签已推送且需要修改源码、工作流或发行配置：修复后走完整前置检查，并创建下一枚 beta 标签；不得删除、移动或覆盖旧标签。
- 已发布 beta 发现严重问题：保留旧 Release 和标签以便审计，在 Release 说明中标记问题或撤回建议；修复后发布下一枚 beta，不回收或重用版本号。
