# `.eclplugin` 归档格式 v1

`.eclplugin` 是无签名的 ZIP 归档，不是加密文件。插件在启动器主进程中运行；Python 依赖只安装到数据目录中的独立库目录，不创建额外解释器、venv 或 Worker。安装只校验和准备文件，默认重启后加载。目录隔离不提供同名包不同版本的并行导入，也不是权限或资源沙箱。不要把归档哈希误认为作者身份认证。

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

插件依赖必须与宿主生产依赖以及本进程已登记包版本兼容，不得替换标准库、ECL 或 SDK。不同包提供同名导入也视为冲突。启动前整体检查归档组合，冲突双方均保持禁用。可以保存冲突插件，但不会执行其入口，界面显示原因。

安装/更新状态为待重启，不热切换已导入的依赖。禁用、卸载和代码重载不清空 `sys.modules`，不能卸载原生扩展或强制终止插件线程。卸载撤销活动指针，但保留代码、依赖目录和历史记录；本版不自动清理旧 runtime/venv。旧 Worker 指针迁移仅在原始代码和离线依赖完整时提交新记录，失败保留旧数据并显示错误，不启动旧 Worker；可显式允许联网重装来补齐缺失 wheel。

## 制作与检查

在主仓库环境中运行：

```powershell
.venv\Scripts\python.exe packaging\build-eclplugin.py <插件源目录> <输出路径.eclplugin>
```

输出文件不能位于插件源目录内。制作命令不解析、下载或锁定依赖；作者须自行准备需要随包携带的锁文件与 wheels。

## 主进程 SDK

归档插件可以继续使用启动器内置的 `ecl_plugin_sdk.Plugin`，保持无参数构造和命令契约。命令用装饰器或在构造/`on_load` 中 `register_command` 登记，参数和结果应可 JSON 编码。只有登记命令可以被调用，禁用时拒绝调用；普通公开方法不自动暴露。

```python
from ecl_plugin_sdk import Plugin as BasePlugin


class Plugin(BasePlugin):
    @BasePlugin.on_command("greet")
    def greet(self, name: str) -> dict[str, str]:
        return {"message": f"你好，{name}"}
```

该兼容 SDK 只承诺既有命令能力。需要完整宿主 API 的新归档插件可直接使用 `ECL.plugins.plugin.Plugin`，构造参数、权限声明、生命周期、事件、设置和前端资源与目录插件相同，不需要跨进程代理。代码、依赖及钩子都与启动器共享进程状态。详见[当前实施方案](plugin-install-directory-isolation-plan.md)。

## 产物验证

Windows CPython 3.13 / PyInstaller 单文件产物已验证离线 wheel 安装、重启加载、包资源读取及 `xxhash 3.6.0` 原生导入。这不是所有原生包的通用兼容保证。构建工作流对实际 Nuitka/PyInstaller 产物运行同一用例，测试 wheel 仅用于验收，不会发布为插件运行环境：

```powershell
$env:ECL_PLUGIN_SMOKE_EXECUTABLE = '<实际启动器可执行文件>'
$env:ECL_PLUGIN_SMOKE_WHEELS = '<仅包含当前宿主 xxhash 3.6.0 wheel 的目录>'
.venv\Scripts\python.exe -m pytest tests/test_packaged_plugin_directories.py -q
```
