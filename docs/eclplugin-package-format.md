# `.eclplugin` 归档格式 v1

`.eclplugin` 是无签名的 ZIP 归档，不是加密文件。当前实现提供作者侧制作命令、完整性预检、依赖锁检查、独立环境准备，以及 Worker 验证后的版本化活动指针。插件管理器可在启动时恢复已提交的归档，并提供列表、信息、启用、禁用、重载、卸载和显式注册的命令调用；后端 IPC 与 Dev Channel 已支持预检和显式确认后的归档安装/更新。插件页可选择 `.eclplugin` 文件，展示预检结果，并在用户确认来源未验证后安装；安装期间仅显示等待状态，进度与取消协议、其他 Worker SDK 扩展点代理仍按[实施方案](plugin-dependency-isolation-installation-plan.md)接入。不要把归档哈希误认为作者身份认证。

后端可用 `plugin_package_inspect` 预检本地文件，再经 `plugin_install` 传入 `plugin_path`、`confirm_unverified_source: true`、`allow_network` 及可选的 `offline_runtime_pack` 安装。Dev Channel 对应 `plugin.inspect` 与 `plugin.install`，路径字段为 `path`。未允许联网时，只复用已缓存运行时或显式提供的离线运行时包，并从包内 wheels 与共享缓存安装锁定依赖。

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
    {"path": "main.py", "sha256": "<64 位小写十六进制 SHA-256>", "size": 123},
    {"path": "plugin.json", "sha256": "<64 位小写十六进制 SHA-256>", "size": 456}
  ],
  "format_version": 1
}
```

文件路径使用相对 POSIX 格式。归档不接受符号链接、显式目录条目、路径穿越、大小写折叠后重名、加密条目或 `package-signature.json`。本版没有签名或验签能力；安装界面会警告来源未验证，并要求用户显式确认。

## Python 依赖锁

`plugin.json` 中的 `pythonDependencies` 是 Python 直接依赖字符串数组，与表示插件间依赖的 `dependencies` 完全独立。若数组非空，当前目标的 `locks/<target>.txt` 必须存在，并锁定每一个直接依赖。目标标签固定为 `<os>-<arch>-cp312`，如 `windows-x86_64-cp312`、`windows-arm64-cp312`、`linux-x86_64-cp312`、`linux-arm64-cp312`、`darwin-x86_64-cp312`、`darwin-arm64-cp312`。归档可只支持部分目标；缺少当前目标锁时不能安装含 Python 依赖的插件。

锁文件是 UTF-8 文本，每个包占一行，列出**全部传递依赖**的精确版本和允许的 wheel SHA-256；可有空行或整行注释，不接受索引配置、任意 URL、版本范围、editable 或源码构建。示例：

```text
example-core==2.1.0 --hash=sha256:<64 位小写十六进制摘要>
example-data==1.3.0 --hash=sha256:<摘要一> --hash=sha256:<摘要二>
```

哈希必须对应最终 wheel 文件，而非解包后内容。`wheels/<target>/` 只允许 `.whl` 文件，缺少的 wheel 在用户允许联网时由环境池按锁定哈希补齐；不允许联网时只用包内 wheel、共享缓存及已缓存或显式提供的离线运行时包。当前制作命令不会替作者解析依赖或生成锁，作者需先准备完整锁和目标 wheel。插件页可切换是否联网；当前目标运行时缺失时，也可选择离线运行时包。

## 制作与检查

在主仓库环境中运行：

```powershell
.venv\Scripts\python.exe packaging\build-eclplugin.py <插件源目录> <输出路径.eclplugin>
```

输出文件不能位于插件源目录内。制作命令不解析、下载或锁定依赖；作者须自行准备需要随包携带的锁文件与 wheels。

## Worker 命令 SDK

归档插件可从专用运行时导入 `ecl_plugin_sdk.Plugin`。命令可用装饰器声明，也可在实例化或 `on_load` 阶段调用 `register_command` 登记；参数与结果须能用 JSON 编码。启动器仅通过 `插件名:命令名` 调用已登记命令，禁用插件时拒绝调用，普通公开方法不会自动暴露。

```python
from ecl_plugin_sdk import Plugin as BasePlugin


class Plugin(BasePlugin):
    @BasePlugin.on_command("greet")
    def greet(self, name: str) -> dict[str, str]:
        return {"message": f"你好，{name}"}
```

这一版 SDK 只覆盖命令。目录插件的宿主 `framework` 对象不会传入 Worker；事件、设置、前端资源和其他服务代理尚不能作为归档插件能力使用。
