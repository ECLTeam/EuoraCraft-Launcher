# 依赖、打包与分发

## 清单

第三方清单必须包含 `api_version: 1`、合法 name、版本和包内 `entry_point`。路径形式为 `module/submodule:PluginClass`，宿主解析为包内 Python 文件，不接受任意导入表达式。

`dependencies` 表达插件间版本关系，可以是版本约束字符串或含 version/optional 的对象；它决定加载关系，不会自动从商店安装插件。

`pythonDependencies` 是 Python 直接库需求，与插件依赖独立；完整传递依赖写入目标锁文件。

## Python 库与 ABI

```text
plugin.json
main.py
locks/windows-x86_64-cp311.txt
wheels/windows-x86_64-cp311/*.whl
```

目标标签为 `<windows|linux|darwin>-<x86_64|arm64>-cp<主次版本>`，以实际宿主为准，不能把 cp311 锁直接用于 cp312。

锁逐行使用精确 `package==version --hash=sha256:...`，列出全部传递依赖和允许的 wheel 摘要。作者自行在开发环境解析依赖；启动器不现场求解、运行源码构建、执行 pip 配置或使用任意私有索引。

安装按包内 wheels、共享缓存查找；只有明确允许联网后，才从固定 PyPI 来源补齐锁内兼容 wheel。未允许联网时缺依赖明确失败。

宿主复用发行物解释器，库安装到独立目录，不为每个插件建立 venv，不写入宿主或系统 site-packages。相同锁及 ABI 可共享环境，不同 Worker 可使用同名库的不同版本。

wheel 要校验元数据、RECORD、Requires-Python、宿主标签及完整依赖。`.pth`、启动定制模块和不支持的脚本布局会拒绝；原生扩展仍需实际 ABI 和动态库兼容。

## 制作 `.eclplugin`

```powershell
python packaging/build-eclplugin.py <插件源目录> <输出目录>/example.eclplugin
```

输出目录不能位于源目录内。工具生成 `package-manifest.json` 的逐文件 SHA-256 和大小；该清单不应手写放进插件源码目录，也不会替作者求解库依赖。

归档为无签名 ZIP，不是加密格式。拒绝路径穿越、符号链接、大小写折叠后重名、加密条目及不符合规范的结构。详细格式见[插件包协议](../../protocols/eclplugin-package-format.md)。

## 安装、更新与启用

归档先预检，再确认来源未验证及是否允许联网。安装仅准备文件，不执行入口。安装/更新按结果重启后由用户决定是否启用。

新版本使用活动指针和事务保护旧安装。不同内容不继承旧授权；卸载重装使用新安装身份。损坏数据明确失败并保留，不回退到第三方主进程运行。

开发模式只用于目录源码迭代。关闭模式恢复普通内容摘要检查，开发会话凭据不能打进归档。

## 发布给其他人

当前没有签名、发行人信任根和撤销验证；不能把哈希写成“作者身份已验证”。也没有完整在线发现/更新客户端闭环，本地归档安装不等同插件商店上线。

请随包提供版本说明、支持平台/ABI 和实际测试范围；不携带真实令牌、账户文件、个人目录或用户存档。
