---
name: maintain-ecl-backend

description: Maintain and refactor the EuoraCraft-Launcher (ECL启动器) Python backend consistently. Use for changes under ECL/, backend architecture reviews, IPC additions, account/game/plugin services, dependency injection, lifecycle management, Chinese Docstrings, module organization, Ruff configuration, or backend tests.
---

# ECL启动器后端维护

## 工作流程

1. 读取相关实现、测试、`pyproject.toml` 和 `.gitignore`，保留已有用户修改。

2. 先确定职责和生命周期边界，再决定修改现有文件、合并模块或拆分模块。

3. 对 IPC 输入使用 `ECL.api.models` 的 Pydantic 模型；业务层使用明确类型，不接收原始 `Any` body。

4. 通过 `ApplicationContext` 注入配置、事件、服务和共享资源；禁止新增单例或服务定位器。

5. 为公共类、公共方法、复杂私有方法和资源边界补充中文多行 Docstring。

6. 在组合根、外部请求、后台任务与资源生命周期边界记录可诊断日志，禁止记录令牌和密码。

7. 运行 `scripts/audit_backend.py`，处理所有错误项。

8. 运行 Ruff、`compileall` 和 pytest；涉及 `ECL/minecraft/game` 时同时检查子模块状态和测试。

## 模块组织

- 保持 `ECL/api`、`adapters`、`common`、`events`、`game`、`plugins`、`services`、`utils` 小写命名。

- 优先把同一业务聚合服务保留在一个文件，例如账户聚合逻辑放在 `services/accounts.py`。

- 仅当一个模块包含两个以上可独立演进、独立测试或拥有不同资源生命周期的职责时拆分。

- 不为单个类、少量转发方法或“目录看起来整齐”单独建包。

- `services/authlib.py` 可独立，因为它封装 Yggdrasil/Authlib 协议和令牌持久化；`avatars.py` 可独立，因为它封装图像与网络资源。

- `services/game` 按目录扫描、安装任务和启动生命周期分组；外部只导入 `GameService`。

- 主仓库只能从 `ECL.minecraft.game` 公共入口导入，不直接引用内部能力包。

- `utils` 仅容纳无业务归属的技术能力，不创建 `helpers.py`、`misc.py` 或 `common_utils.py`。

详细边界见 [references/architecture.md](references/architecture.md)。

## 注释和 Docstring

- 使用中文说明“为什么存在、输入约束、返回语义、异常或生命周期”，不要复述方法名。

- 禁止单行 `"""获取配置。"""` 形式。

- 有参数的方法使用多行格式并列出有业务意义的参数；存在返回值时说明返回语义。

- 简单私有转换函数无需为了覆盖率强加 Docstring；复杂算法、回调、线程入口、资源关闭和安全边界必须说明。

- 属性注释放在初始化赋值上方，解释状态所有权和生命周期，不逐行翻译变量名。

- 注释必须随代码更新；无法提供额外信息的注释应删除。

模板和例子见 [references/docstrings.md](references/docstrings.md)。

## 依赖与生命周期

- 事件总线只传递事件；`subscribe` 返回取消订阅函数。

- 应用资源由 `create_application()` 创建，由 `ApplicationContext.close()` 逆序且幂等关闭。

- 后台 `Task`、线程、进程和 HTTP 客户端必须有明确所有者；不得丢弃 `create_task()` 返回值。

- 写入配置、账户、版本元数据和下载目标时使用同目录临时文件与原子替换。

- 解压、删除和卸载前验证目标路径仍位于授权根目录。

## 日志

- 启动时只调用一次 `configure_logging()`，业务模块通过 `get_logger()` 或标准库命名日志器取用配置。

- 完整日志文件始终保留 `DEBUG` 记录；Debug 开关控制控制台详细程度，不应导致故障上下文从文件中消失。

- 在依赖构造、缓存命中、任务创建/取消、插件加载顺序、进程启动和资源关闭处记录 Debug 日志。

- 日志使用参数化格式，禁止拼接或记录访问令牌、刷新令牌、密码、设备码和完整启动命令。

- 可预期的业务失败记录稳定错误码；未知异常使用 `logger.exception()` 保留堆栈。

## 验收

在仓库根目录执行：

```powershell

python .agents/skills/maintain-ecl-backend/scripts/audit_backend.py .

python -m ruff check ECL tests

python -m compileall -q ECL tests

python -m pytest -q

```

只报告实际运行过的 Python 版本；目标版本语法检查不能冒充对应解释器上的完整 pytest。
