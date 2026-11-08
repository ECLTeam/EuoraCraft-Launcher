# 后端结构边界

## 组合根

- `ECL/application.py`：唯一依赖组合入口、运行状态和资源关闭顺序。

- `ECL/launcher.py`：应用宿主，不负责发现或注册业务服务。

- `ECL/events/event_bus.py`：同步事件分发，不保存服务实例。

## API

- `ECL/api/models.py`：IPC 请求模型和枚举。

- `ECL/api/domain_handlers.py`：类型校验、错误转换和服务调用。

- `ECL/api/registry.py`：命令名到处理器的统一映射。

- `ECL/api/legacy/`：兼容旧前端的分域适配层，不承载业务规则。

## Services

- `accounts.py`：账户聚合、当前账户和 Microsoft 启动器适配。

- `authlib.py`：Yggdrasil/Authlib 协议、账户令牌和 Injector 资源。

- `avatars.py`：头像获取、皮肤渲染和图像缓存。

- `game/`：游戏目录、扫描、安装和进程生命周期协调器。

- `info_card.py`、`maintenance.py`：独立且轻量的业务能力。

不要把一个聚合服务按每个动作拆成文件。拆分必须满足至少一项：不同协议边界、不同资源所有者、可独立替换、可独立测试，或文件已同时包含多个高复杂度工作流。

## Game

`ECL/game` 是独立子模块和稳定公共门面。内部按 `auth`、`catalog`、`download`、`files`、`install`、`java`、`launch`、`network`、`utils` 组织。主仓库不得导入 `ECL.game.<内部包>`。

## Plugins

`PluginManager` 是唯一公开管理器。发现、持久化、生命周期和注册表可分模块，但插件调用方不感知这些内部类型。

## Utils

只放配置存储、环境读取、日志配置、运行路径和原子文件操作。业务枚举、账户规则、游戏目录规则不能放入 `utils`。
