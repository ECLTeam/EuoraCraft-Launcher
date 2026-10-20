# `.eclplugin` 归档格式 v1

`.eclplugin` 是无签名 ZIP 归档，不是加密文件。第三方插件使用 API v2，在独立 Worker 中运行；系统插件仍由宿主维护。安装只准备文件，不自动审阅、授予权限或执行入口。依赖安装到独立库目录，不另建解释器或 venv；Worker 复用当前发行物。归档摘要用于完整性校验，不证明作者身份。Worker 不提供恶意 Python 的操作系统文件或网络沙箱。

后端用 `plugin_package_inspect` 预检，再通过 `plugin_install` 传入 `plugin_path`、`confirm_unverified_source: true` 和 `allow_network` 安装。Dev Channel 对应 `plugin.inspect` 和 `plugin.install`，路径字段为 `path`。预检返回 `dependencies_ready`；安装结果包含 `status` 和 `message`，列表提供 `pending_restart` 与 `installed_version`（Dev Channel 为 `pendingRestart`、`installedVersion`）。不再接受运行时离线包参数。未允许联网时仅使用包内 wheels、共享 wheel 缓存和已就绪依赖目录。

## 文件结构

插件代码仍按现有 `plugin.json` 的 `entry_point` 约定放在归档根目录。`package-manifest.json` 由打包命令生成，不能放进插件源目录。

```text
sample.eclplugin
  plugin.json
  main.py
  resources/...
  locks/<target>.txt      # 当前目标完整传递依赖锁
  wheels/<target>/*.whl   # 可选，当前目标离线 wheel
  package-manifest.json
```

清单为 UTF-8 JSON，`format_version` 固定为 `1`，`files` 枚举除清单自身之外的所有普通文件：

```json
{
  "files": [
    { "path": "main.py", "sha256": "<64 位小写十六进制 SHA-256>", "size": 123 },
    {
      "path": "plugin.json",
      "sha256": "<64 位小写十六进制 SHA-256>",
      "size": 456
    }
  ],
  "format_version": 1
}
```

文件路径使用相对 POSIX 格式。归档不接受符号链接、显式目录条目、路径穿越、大小写折叠后重名、加密条目或 `package-signature.json`。本版没有签名或验签能力；安装界面会警告来源未验证，并要求用户显式确认。

## Python 依赖锁

`plugin.json.pythonDependencies` 是 Python 直接需求字符串数组，与插件间的 `dependencies` 完全独立。需求可以包含版本范围和 extras，但首版不支持需求中的 URL 或环境标记；平台差异通过各目标完整锁表达。目标标签按实际宿主确定：`<os>-<arch>-cp<主次版本>`，例如 Python 3.12 的 `windows-x86_64-cp312`，Python 3.13 的 `windows-x86_64-cp313`。系统取 `windows`、`linux`、`darwin`；架构取 `x86_64`、`arm64`。缺少当前目标锁时不能安装含 Python 依赖的插件。cp312 锁不能在 cp313 宿主上冒充兼容。

锁文件是 UTF-8 文本，每个包占一行，列出**全部传递依赖**的精确版本和允许的 wheel SHA-256；可有空行或整行注释，不接受索引配置、任意 URL、版本范围、editable 或源码构建。示例：

```text
example-core==2.1.0 --hash=sha256:<64 位小写十六进制摘要>
example-data==1.3.0 --hash=sha256:<摘要一> --hash=sha256:<摘要二>
```

哈希对应最终 wheel 文件，而非解包后内容。`wheels/<target>/` 只允许 `.whl`。启动器按包内 wheel、共享缓存的顺序查找；显式允许联网后才从固定 PyPI Simple JSON 索引补齐锁内精确版本与摘要的兼容 wheel。没有现场版本求解、源码构建、pip 配置或私有索引支持。作者应使用自己的开发环境准备完整锁与 wheels；制作命令不代替作者求解依赖。

准备时检查 wheel 元数据、RECORD、Requires-Python、宿主标签、完整传递依赖及 extras 闭包。`.pth`、sitecustomize/usercustomize、原始脚本布局不支持并拒绝安装；console/gui scripts 不生成解释器包装器，安装结果明确提示仅支持库导入。原生扩展仍依赖实际宿主 ABI 和动态库兼容性。

## 安装与运行边界

同一完整锁字节、宿主 ABI 和平台共用 `plugin_deps/<key>/site-packages`；不同锁共享 `plugin_cache/wheels/<sha256>`，不向系统或宿主 site-packages 安装。就绪目录逐文件复核，禁止增量覆盖损坏目录；代码版本位于 `plugin_packages/<name>/pkg-<摘要>`，活动指针为 `active.json`。

不同 Worker 可以使用同名包的不同版本；每个 Worker 校验自己的完整锁、代码和依赖目录。不得覆盖标准库、ECL 或 API，单一环境内不同包提供同名导入仍拒绝。原生扩展须匹配当前解释器 ABI。

归档安装和更新仍显示待重启；重启后也必须通过内容审阅和必要授权才能启用。目录安装完成后同样待审阅。授权绑定宿主安装身份、代码、权限、完整锁和 ABI；更新内容不会自动继承许可，卸载重装使用新安装身份。损坏数据保留原文件并明确失败，不转换旧权限为 allow，也不回退到第三方主进程运行。

禁用、重载和撤销通过 Worker 监管处理，确认旧进程树停止后才建立新运行。已完成的设置写入不会因权限撤销自动回滚。设置和授权原文件保留，详细状态见插件管理页。

## 制作与检查

在主仓库环境中运行：

```powershell
.venv\Scripts\python.exe packaging\build-eclplugin.py <插件源目录> <输出路径.eclplugin>
```

输出文件不能位于插件源目录内。制作命令不解析、下载或锁定依赖；作者须自行准备需要随包携带的锁文件与 wheels。

## 第三方 API v2

目录与归档共用清单和 SDK；`plugin.json` 必须显式声明 `api_version: 2`。示例：

```json
{
  "name": "example",
  "api_version": 2,
  "version": "1.0.0",
  "entry_point": "main:Plugin",
  "permissions": [
    { "capability": "commands.register", "required": true },
    { "capability": "commands.execute", "required": true }
  ]
}
```

```python
from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    def on_load(self):
        self.define_command(
            "greet", self.greet,
            input_fields=({"name": "name", "type": "string"},),
            output_fields=({"name": "message", "type": "string"},),
        )

    def greet(self, name):
        return {"message": f"Hello, {name}"}
```

插件获得 HostClient，不能取得宿主 framework、账户服务或配置对象。回调留在 Worker 内，跨进程只传 JSON。当前已开放自身命令、设置、自查询和自身设置变化订阅；尚未接线的业务方法明确失败，依赖未实现必要能力的插件不可启用。第三方自定义前端保持关闭。

设置写入必须提供 `expected_revision`；变更 RPC 使用本运行 `mutation_key` 去重，同键不同内容拒绝。等待超时不能当作动作已经停止或回滚，也不能无条件换新键重放副作用。

命令字段 schema 当前只接受 `string`、`number`、`integer`、`bool`，支持 `required`、`max_length`、`minimum`、`maximum`、`enum`，不执行插件验证器、不支持任意 JSON Schema 引用或嵌套对象。每组最多 32 个字段，单运行最多 64 个命令、32 个订阅；请求和结果额外限制为 64 KiB。设置支持 `bool`、`string`、`number`、`select`，所有写入经定义校验并原子提交。

需要超时后核对写入的调用方应显式保留原 `mutation_key`，通过 `host.call("mutations.get", {"mutation_key": key})` 查询同一运行的 `pending`、`succeeded` 或 `failed`，结果包含 `result` 和 `error_code`。回执最多保留 1024 个，容量满时拒绝新变更；不淘汰有效回执后重放。授权 epoch 改变后不交付旧回执结果；run 结束后回执不再可用。SDK 自动生成的键不会构成自动重试承诺。

撤销首先阻止新提交；已接受的设置写入可能继续落盘。宿主在途写入、Worker 和事件资源均结束后才报告清理完成，超时只报告 pending 或失败。事件队列满及可靠事件交付失败会关闭该 Broker 并回收运行；已经提交的设置仍报告真实 revision，不伪造回滚。

完整示例见 [worker_echo](../examples/plugins/worker_echo/README.md)。现行运行边界见 [无头运行说明](headless-runtime.md)，总体契约与后续能力见 [Worker/API v2 设计](plugin-worker-api-v2-implementation-design.md)。`ECL.plugins.Plugin` 仅供发行资源中的可信系统插件使用，不为第三方提供旧 SDK 适配。

## 产物验证

真实冻结引导、依赖和生命周期用例位于 `tests/test_plugin_worker_bootstrap.py`、`test_plugin_worker_dependencies.py` 和 `test_plugin_worker_lifecycle.py`；正式管理器流程位于 `test_plugin_worker_manager.py` 与 `test_plugin_manager_packages.py`。测试报告区分源码、真实冻结 Worker 和真实桌面主窗口，不把历史版本结果作为当前代码的验证。
