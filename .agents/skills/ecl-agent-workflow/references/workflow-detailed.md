# 详细工作流规程

本文件是 SKILL.md 中六个阶段的具体执行规程，当阶段细节需要展开时读取。

## 1. 考察阶段 (Inspect)

### 必须读取的文件类别

按优先级读取：

1. **直接相关源文件**：变更目标文件的全部内容。

2. **接口契约**：后端 `ECL/api/models.py`、`ECL/api/registry.py`；前端 `types/api.ts`。

3. **测试文件**：对应模块的 `tests/test_*.py` 或 `frontend/src/**/*.test.ts`。

4. **配置**：`pyproject.toml`（ruff/pytest/版本）、`frontend/package.json`（lint/typecheck/test 脚本）。

5. **文档**：`docs/` 下相关 ADR，`.memory/INDEX.md` 了解当前任务状态。

6. **子模块状态**：`git submodule status` 查看子模块当前指针。

### 考察输出

在 commentary 中输出考察摘要，格式：

```

[考察] 涉及模块：ECL/api/game.py, ECL/services/game/launch.py

[考察] 已读文件：5 个源文件 + 2 个测试文件 + 1 篇 ADR

[考察] 关键发现：

  - GameService 已暴露 launch_game 方法，复用无需修改

  - 现有测试覆盖了正常启动路径，未覆盖取消路径

  - ADR-010 记录了此前对启动流程的变更

```

## 2. 规划阶段 (Plan)

### 计划必须包含的内容

```markdown
## 变更计划

### 目标

一句话描述要解决的问题或要添加的功能。

### 涉及文件

- 后端：`ECL/api/game.py`（新增 IPC 命令）

- 前端：`frontend/src/features/instances/api/instanceRuntimeApi.ts`（新增调用）

### 影响范围

- 影响实例启动流程

- 不影响插件系统

- 不涉及子模块变更

### 兼容性

- 向后兼容：是，新增可选参数，默认行为不变

- IPC 协议变更：新增 `game_cancel_launch` 命令

### 测试策略

- 后端：新增 `test_launch_cancellation.py`，覆盖正常取消和重复取消

- 前端：更新 `InstanceDetailModal.test.ts`，验证取消按钮状态
```

### 跨前后端变更额外要求

- 标明 IPC 命令名，确认与 `ECL/api/registry.py` 中的注册一致。

- 前端 DTO 定义必须与后端 `ECL/api/models.py` 的 Pydantic 模型对齐。

- 如果是新增 IPC 命令，在 `ECL/api/registry.py` 中注册。

## 3. 编码阶段 (Code)

### 编码前检查清单

- [ ] 已经确认复用机会（搜索现有实现，不重复造轮子）

- [ ] 后端 IPC 输入使用了 Pydantic 模型而非原始 dict

- [ ] 新命令已注册到 `registry.py`

- [ ] 异常处理遵循 `ECL/utils/errors.py` 中的模式

- [ ] 日志记录遵循项目日志规范（参数化格式，不记录敏感信息）

- [ ] 前端 API 调用走 feature 层，不在组件中直接 invoke

- [ ] 前端组件复用了现有 `UiButton`、`UiCard`、`UiInput` 等

- [ ] 不创建孤立工具函数——检查能否放入 `ECL/utils/` 或前端 `utils/` 中已有模块

### 编码约束

- 后端方法签名禁止使用 `**kwargs` 透传，必须显式声明参数。

- 禁止在业务代码中捕获 `BaseException` 或 `Exception` 后静默忽略。

- 文件操作必须验证路径在授权根目录内（参考 `ECL/utils/files.py` 中的安全模式）。

- 前端组件 props 使用 TypeScript 接口定义，不写 `defineProps({ ... })` 运行时声明。

- 后端新增依赖必须先在 `pyproject.toml` 中添加，通过 `pip install -e ".[dev]"` 安装。

## 4. 测试阶段 (Test)

### 后端测试

```powershell

# 全量测试

python -m pytest -q



# 指定模块

python -m pytest tests/test_game_service.py -q -v



# 带覆盖率

python -m pytest --cov=ECL --cov-report=term

```

### 前端测试

```powershell

cd frontend

pnpm test            # 全量

pnpm test -- --run   # 非 watch 模式

```

### 代码检查

```powershell

# 后端

python -m ruff check ECL tests



# 前端

cd frontend

pnpm lint

pnpm typecheck

```

### 测试编写原则

- 新增功能必须添加测试，不以下游已有测试覆盖为借口不写。

- 测试应为隔离的：使用 mock 替代外部依赖（网络、文件系统、子进程）。

- 测试命名：`test_<功能>_<场景>_<预期结果>`。

- 异常路径和边界条件必须测试，仅测试 happy path 不算完成。

## 5. 提交阶段 (Commit)

### 提交粒度

| 阶段 | 应提交 | 不应提交 |

|------|--------|----------|

| 考察 | 文档变更（如 ADR） | 代码变更 |

| 规划 | 计划文档 | 代码变更 |

| 编码 | 代码 + 对应测试 | 调试代码 |

| 测试 | 测试修复 + 测试新增 | 未通过测试的代码 |

| 文档 | ADR 文档 | 无 |

### 提交信息格式

```

<type>(<scope>): <简要描述>



<详细说明（可选，用空行分隔）>

```

示例：

```

feat(backend): 新增游戏启动取消 IPC 命令



- 新增 game_cancel_launch 命令注册到 registry

- 取消时发送 SIGNAL 而非轮询状态

- 新增 test_launch_cancellation.py 覆盖正常/异常路径

```

### 提交前检查

- [ ] 没有遗留的 `print()` / `console.log()` / `TODO`

- [ ] 没有未使用的 import

- [ ] 没有硬编码的令牌、密钥、密码

- [ ] 没有未注册的 IPC 命令

- [ ] 没有与现有测试冲突的变更

- [ ] 前端没有 ESLint 错误和 TypeScript 错误

## 6. 记录阶段 (Document)

### 需要记录决策的场景

- 新增或修改 IPC 协议

- 重构模块结构（拆分、合并、移动）

- 引入新的外部依赖

- 变更现有测试策略

- 架构选型（库选择、设计模式选择）

- 放弃某条技术路线

### 不需要记录的场景

- 修复显而易见的 bug（如拼写错误、空指针检查）

- 微小的样式调整

- 依赖版本升级（仅限 patch 版本）
