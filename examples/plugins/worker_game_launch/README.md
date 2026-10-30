# 获准游戏启动示例

加载插件仅注册命令。先在可信主窗口为具体实例分别批准读取、启动和停止；根目录不能代替启动或停止的实例范围。首轮仅支持 Windows x64 本地 NTFS。

1. 执行 `discover(capability="instances.launch")`，明确选择一个 `instance_ref`。
2. 执行 `launch(instance_ref,mutation_key)`，用回执的 `operation_ref` 执行 `status`。
3. 成功后用 `running(instance_ref)` 查询本运行创建的进程。
4. 执行 `stop(game_run_ref,mutation_key)`，继续查询停止任务，直至实际结束。

账户和Java由宿主选择；示例不接收凭据、Java路径、JVM参数、命令、脚本或环境变量。相同提交重用原去重键；不要在等待超时后换键重放。取消启动不等于游戏已经停止。进程创建后撤权或关闭插件不会杀游戏，仍可从启动器正常管理；新Worker不能接管旧引用。只允许停止当前运行自己创建的确切进程。

[接口与边界](../../../docs/protocols/eclplugin-package-format.md#获准游戏目录安装与进程控制)
