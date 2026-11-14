---
name: ecl-agent-workflow

description: Enforce a rigorous inspect → plan → code → test → commit → document workflow for AI agents working on EuoraCraft-Launcher (ECL启动器). Use for any backend (ECL/), frontend (frontend/src/), game core (ECL/minecraft/game/), submodule, CI, or packaging change. Not for standalone one-off scripts or third-party dependency bumps without logic changes.
---

# ECL启动器 Agent Workflow

本 skill 定义 AI agent 在 EuoraCraft-Launcher（ECL启动器）项目上工作的强制性流程规范，防止"vibe coding"导致的代码逻辑混乱、过度拆分、随意自设计、缺乏复用、风格不统一等问题。

## 核心原则

- **先读后写**：任何修改前，必须先阅读相关现有代码、测试和文档。

- **计划先行**：编码前必须输出书面计划，明确变更范围和影响。

- **代码即工程**：复用现有模式、工具函数、DTO 模型和组件，不重新发明轮子。

- **测试伴随**：每个功能变更必须伴随测试（后端 pytest，前端 vitest）。

- **逐段提交**：每一步完成后提交 git，提交信息遵循 Conventional Commits。

- **决策留痕**：每个决策必须输出文档到 `docs/`，格式为 ADR（Architecture Decision Record）。

## 强制工作流

每个变更必须依次经过以下阶段，跳过或合并阶段需要显式说明理由：

```

1. 考察 (Inspect)  →  2. 规划 (Plan)  →  3. 编码 (Code)  →  4. 测试 (Test)  →  5. 提交 (Commit)  →  6. 记录 (Document)

```

### 1. 考察 (Inspect)

在修改任何代码前，必须：

- 读取变更涉及的全部现有源文件，理解其职责、边界和调用链。

- 读取相关测试文件，理解现有测试覆盖和测试模式。

- 读取相关文档（`docs/`、`.memory/`、`README.md`）。

- 读取 `pyproject.toml` / `package.json` 中的 lint 和类型检查配置。

- 如果有现成的 skill（`maintain-ecl-backend`、`maintain-ecl-frontend`），先加载其指令。

输出：考察摘要，列出读过的文件和关键发现。

### 2. 规划 (Plan)

- 基于考察结果，输出书面变更计划。

- 计划必须包含：变更目标、涉及文件列表、影响范围、向后兼容性评估、测试策略。

- 对于跨前后端的变更，必须说明 IPC 接口变化和 DTO 对齐策略。

- 对于涉及子模块的变更（`ECL/minecraft/game`、`frontend`），必须评估子模块版本约束。

输出：变更计划文档（可直接写入 `docs/` 或在 commentary 中输出）。

### 3. 编码 (Code)

- 遵循 [references/coding-standards.md](references/coding-standards.md) 中的编码规范。

- 优先复用现有代码：`ECL/utils/`、`ECL/api/models.py`、`ECL/common/`、前端 `composables/`、`utils/`、`components/`。

- 后端 IPC 输入使用 `ECL.api.models` 的 Pydantic 模型；业务层使用明确类型，不接收原始 `Any` body。

- 前端 API 调用走 `features/*/api/` 层，不直接在组件中调用 `invoke`。

- 不创建没有明确业务归属的 `helpers.py`、`misc.py`、`common_utils.py`。

- 不为单个类或少量转发方法单独建包。

- 新增公共 API 时必须注册到 `ECL/api/registry.py`。

### 4. 测试 (Test)

- 后端：运行 `pytest -q` 确保全部测试通过。新增功能必须添加对应测试。

- 前端：运行 `pnpm test` 确保全部测试通过。

- 涉及子模块时，检查子模块测试状态。

- 运行 lint：后端 `ruff check ECL tests`，前端 `pnpm lint`。

- 前端同时运行 `pnpm typecheck`。

### 5. 提交 (Commit)

- 每一步完成后必须提交 git，不允许累积多个阶段的变更一次性提交。

- 提交信息遵循 Conventional Commits 格式：`<type>(<scope>): <description>`

- 允许的 type：`feat`、`fix`、`perf`、`refactor`、`test`、`docs`、`style`、`chore`。

- scope 参考：`backend`、`frontend`、`game-core`、`ci`、`docs`、`plugin`、`packaging`。

- 提交前检查：不要提交调试代码、TODO 注释、未使用的 import、密码/令牌。

### 6. 记录 (Document)

- 每个有意义的决策（架构选型、API 设计变更、重构策略、bug 修复方案）必须以 ADR 格式记录到 `docs/`。

- 模板见 [references/decision-log-template.md](references/decision-log-template.md)。

- 文件名格式：`docs/ADR-<编号>-<简短标题>.md`。

- 编号从 `.memory/INDEX.md` 中已有的最大决策编号+1 开始。

- 如果决策已在 `.memory/decisions/` 中有记录，在 `docs/` 中同步一份。

## 模块边界速查

| 领域 | 位置 | 技术栈 | 测试框架 |

|------|------|--------|----------|

| 后端核心 | `ECL/` | Python 3 | pytest |

| 后端 API | `ECL/api/` | pytaurix IPC | pytest |

| 游戏核心 | `ECL/minecraft/game/` (子模块) | Python | pytest |

| 前端 | `frontend/src/` (子模块) | Vue 3 + TS | vitest |

| 联机插件（已迁出） | 独立仓库 ECL-Plugin-OnlineGame | Python | 其仓库内 pytest |

| 插件系统 | `ECL/plugins/` | Python | pytest |

| CI/CD | `.github/workflows/` | YAML | 无 |

## 现有 skill 联动

- 后端具体维护细则 → `$maintain-ecl-backend`

- 前端具体样式细则 → `$maintain-ecl-frontend`

当变更涉及后端或前端时，先加载对应 skill 的指令再进入编码阶段。

## 验收

在仓库根目录执行：

```powershell

# 后端

python -m ruff check ECL tests

python -m pytest -q



# 前端

cd frontend

pnpm lint

pnpm typecheck

pnpm test

```

所有检查必须通过后方可提交。
