# 发布与构建流程

本项目支持 Alpha、Beta、RC 和正式版。主仓库的版本标签触发完整构建与 GitHub Release 发布，子仓库随主仓库记录的提交一起交付。

## 发布类型与标签

| 类型 | 标签示例 | GitHub 发布标记 |
| --- | --- | --- |
| Alpha | `v1.2.3-alpha.1` | 预发布 |
| Beta | `v1.2.3-beta.1+20261025` | 预发布 |
| RC | `v1.2.3-rc.1` | 预发布 |
| 正式版（Releases） | `v1.2.3` | 正式发布 |

正式版的内部类型值为 `release`。各平台使用同一份核心版本、完整版本及发布类型；带有构建元数据的完整版本原样保留。

Alpha、RC 示例不新增序号或日期限制。Beta 必须使用 `vMAJOR.MINOR.PATCH-beta.N+YYYYMMDD`，遵守以下规则：

- `MAJOR.MINOR.PATCH` 遵循语义化版本；`N` 从 1 起递增，升级基础版本后重置为 1。
- 日期使用 Asia/Shanghai 的实际发布日期，且必须是有效日历日期。
- Beta 使用带注释标签；远端版本标签均不可删除、移动、覆盖或复用。

## 日常检查与完整构建

| 触发方式 | 执行内容 |
| --- | --- |
| `main` 推送、面向 `main` 的 PR | `Backend Tests`：后端及 Core 检查和测试；`Frontend Checks`：当前子模块的 `pnpm check` |
| 版本标签推送 | 发布条件校验、前端构建、Windows x64/ARM64、Linux、macOS、Arch 打包与发布 |
| 手动运行 `Build` | 完整构建并上传 Actions 产物，不创建或更新 Release |

日常推送不执行平台打包。手动运行可选择分支或版本标签；分支构建使用源码中的版本常量，不把分支名当版本。手动标签构建也不会发布。PR 的现有 Ruff 检查及子模块同步工作流继续按各自配置运行。

日常检查成功不等于安装包已经验证；需要提前验包时，在 Actions 中手动运行 `Build`。完整构建仍包含现有前端检查与构建步骤；成品启动、打包后无头运行、插件依赖安装与 Worker 等测试按需单独执行。

## 发布前置条件

1. 完成所需功能、测试和验收，提交全部相关修改，保持工作区干净。
2. 按 [项目规范第 8 节](../../AGENTS.md#8-子模块指针与推送顺序)核对全部子模块指针：先推子模块，再推主仓库 `main`，确认远端均可检出。
3. 等待同一主仓库提交由主分支推送触发的 `Backend Tests`、`Frontend Checks` 成功；以各工作流最近一次运行结果为准。PR、其他分支或其他提交的成功记录不能替代。
4. 确认标签将指向远端 `main` 的当前提交，且该标签在本地和远端均未使用。Beta 另外满足日期、序号和带注释标签要求。

发布门禁会拒绝不支持的版本类型、指向历史提交的标签，以及质量检查缺失、未完成、失败或取消的发布提交；不再要求主分支完整构建的成功记录。

## 发布命令

以下以 Beta 为例，执行前替换为实际版本和发布日期，并确认当前会话已授权推送与发布：

```bash
git -C ECL/game push origin main
git -C ECL/services/florolding push origin main
git -C frontend push origin main
git push origin main
git status --short
git log -1 --oneline origin/main
# 等待该提交的 Backend Tests 和 Frontend Checks 成功后创建标签。
git tag -a v1.2.3-beta.1+20261025 -m "v1.2.3-beta.1+20261025"
git push origin v1.2.3-beta.1+20261025
```

不要使用 `--force`、`--tags` 或 `--follow-tags` 批量推送标签。

## 完成判定与失败处理

标签推送后等待所有平台构建和 Arch 上传完成，在 Release 页面确认预期产物均可下载。前三种发布为 prerelease，正式版为正式 Release；仅工作流启动或部分产物上传不算完成。

发布结果记录标签、目标提交、子模块指针、工作流链接、产物及已知限制。基础设施临时故障可重跑同一标签的失败任务；需要修改源码或发布配置时，修复后使用新的版本标签，不改动旧标签。严重问题可在旧 Release 标记撤回建议，再发布修正版。
