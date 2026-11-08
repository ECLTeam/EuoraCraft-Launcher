# 编码规范与复用原则

## 核心原则：先找再写

编写任何新代码前，先搜索项目中是否已有类似功能的实现。以下是不应重复造轮子的场景：

### 后端

| 若你需要 | 先看这里 |

|----------|----------|

| 文件读写 | `ECL/utils/files.py` |

| 网络请求 | `ECL/utils/network.py` |

| 日志 | `ECL/utils/logging.py` 的 `get_logger()` |

| 错误类型 | `ECL/utils/errors.py` |

| 配置读写 | `ECL/utils/config.py` |

| IPC 输入模型 | `ECL/api/models.py` |

| IPC 命令注册 | `ECL/api/registry.py` |

| 事件发布/订阅 | `ECL/events/event_bus.py` |

| 依赖注入 | `ECL/application.py` 的 `ApplicationContext` |

| 路径解析 | `ECL/common/runtime.py` |

### 前端

| 若你需要 | 先看这里 |

|----------|----------|

| 按钮 | `UiButton` |

| 输入框 | `UiInput` |

| 卡片 | `UiCard` |

| 图标 | `UiIcon` (lucide icons) |

| 布局 | `SectionLayout` |

| 页面布局模式 | `frontend/src/styles/tokens.css` |

| API 调用 | `features/*/api/*.ts` |

| 响应式状态 | `composables/*.ts` |

| 工具函数 | `utils/*.ts` |

| i18n 键 | `i18n/locales/*.json` |

| 类型定义 | `types/api.ts` |

## 模块组织原则

### 后端 (`ECL/`)

- `ECL/api/` — 只放 IPC 命令处理函数，不包含业务逻辑。业务逻辑调用 `services/` 或 `game/`。

- `ECL/services/` — 业务聚合服务，一个文件一个聚合边界。例如 `services/accounts.py` 聚合账户所有操作。

- `ECL/plugins/` — 插件框架和内置扩展点。每个扩展点一个文件（如 `launch_hooks.py`、`network.py`）。

- `ECL/utils/` — 无业务归属的技术能力。不创建 `helpers.py`、`misc.py`、`common_utils.py`。

- `ECL/common/` — 跨模块共享的配置和运行时信息。不包含业务逻辑。

- `ECL/events/` — 事件总线定义。不包含业务逻辑。

拆分规则：

- 仅当模块包含两个以上可独立演进、独立测试或拥有不同资源生命周期的职责时拆分。

- 不为单个类、少量转发方法或"目录看起来整齐"单独建包。

- 新增模块前，先问自己：这个模块是否会被多个地方 import？如果是，放在 utils 还是 services？

### 前端 (`frontend/src/`)

- `features/` — 按功能域组织，每个域包含 `api/`、`components/`、`composables/`。

- `components/` — 跨功能域共享的通用组件。

- `composables/` — 跨功能域共享的组合式函数。

- `utils/` — 纯工具函数，无 Vue 依赖。

- `views/` — 页面级组件，不应包含业务逻辑。

- `types/` — TypeScript 类型定义，与后端 DTO 对齐。

## 命名约定

### 后端 (Python)

| 类别 | 规范 | 示例 |

|------|------|------|

| 模块名 | 小写蛇形 | `game_service.py` |

| 类名 | PascalCase | `GameService` |

| 函数/方法 | 小写蛇形 | `launch_game()` |

| 私有方法 | 单下划线前缀 | `_validate_version()` |

| 常量 | 大写蛇形 | `MAX_RETRY_COUNT` |

| 异常 | PascalCase + Error 后缀 | `GameLaunchError` |

### 前端 (TypeScript)

| 类别 | 规范 | 示例 |

|------|------|------|

| 文件名 | kebab-case | `instance-runtime-api.ts` |

| 组件名 | PascalCase + .vue | `InstanceDetailModal.vue` |

| 接口/类型 | PascalCase | `InstanceProfile` |

| 函数 | camelCase | `fetchInstanceList()` |

| API 函数 | camelCase + Api 后缀 | `launchGameApi()` |

| composable | camelCase + use 前缀 | `useInstanceList()` |

## Python 风格细节

- 缩进 4 空格，行宽 120。

- 引号使用双引号（与 ruff 配置一致）。

- import 顺序：future → 标准库 → 第三方 → 第一方 → 本地文件夹。

- 公有方法必须写中文 Docstring，说明"为什么存在、输入约束、返回语义、异常"。

- 私有方法如果算法复杂同样需要 Docstring。

- 不要写没有信息的单行 Docstring（如 `"""获取配置。"""`）。

- 类型注解必须完整，禁止使用 `Any` 作为参数或返回值类型（与 Pydantic 模型交互的边界除外）。

## TypeScript 风格细节

- 缩进 2 空格，行宽由 prettier 管理。

- 使用 `interface` 而非 `type` 定义对象类型。

- 组件 props 使用 TS 接口 + `defineProps<>()` 泛型，不使用运行时声明。

- 禁止 `any`，优先 `unknown` 再类型收窄。

- 异步函数必须标注返回类型，不依赖隐式推断。

## 文件边界

- 一个文件不超过 500 行（后端）或 400 行（前端）。超过时考虑拆分职责。

- 一个函数不超过 60 行。超过时考虑提取辅助函数。

- 一个组件不超过 400 行模板。超过时考虑拆分子组件。
