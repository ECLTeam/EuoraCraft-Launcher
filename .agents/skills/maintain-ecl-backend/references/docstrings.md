# 中文 Docstring 与属性注释

## 方法模板

```python

def subscribe(self, event: str, handler: EventHandler, owner: str | None = None) -> Unsubscribe:

    """

    将处理器订阅到指定事件，并返回可重复调用的取消函数。



    :param event: 事件名称，例如 ``config:updated``

    :param handler: 事件触发时同步调用的处理器

    :param owner: 可选的资源所有者，用于批量清理插件订阅

    :return: 取消当前订阅的无参数函数

    """

```

## 装饰器模板

```python

def event_handler(event: str):

    """

    装饰器：将方法注册为事件处理器，实例化时自动订阅。



    :param event: 事件名称，例如 ``config:updated``

    :return: 保留原调用签名的注册装饰器

    """

```

## 生命周期模板

```python

def close(self) -> None:

    """

    按依赖逆序释放当前对象拥有的资源；重复调用不会再次关闭资源。



    已启动的外部游戏进程不属于该对象，不在此处强制终止。

    """

```

## 属性注释

```python

# 由当前服务拥有的安装任务；任务结束后必须从映射中移除。

self._install_tasks: dict[str, asyncio.Task[None]] = {}



# 共享 HTTP 客户端由 ApplicationContext 关闭，本对象不得重复关闭。

self.http = http_client

```

避免以下写法：

```python

"""获取配置。"""

self._tasks = {}  # 任务字典

```

前者没有输入、返回或边界信息；后者只是重复变量名。
