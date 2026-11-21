# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证统一基类的装饰器语法糖：@command/@launch_hook/@event
#   静态标记经预置扫描批量登记，与显式 define_* 混用且可被覆盖。
# ============================================================

from __future__ import annotations

import json
from pathlib import Path

from ECL.plugins.worker_service import PluginWorkerService


def _write_plugin(root: Path, metadata: dict, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (root / "main.py").write_text(body, encoding="utf-8")


DECORATED_BODY = (
    "from ECL.plugins.sdk import Plugin, command, launch_hook, event\n"
    "class DecoratedPlugin(Plugin):\n"
    "    @command(is_query=True, description='查询问候',"
    " input_fields=({'name': 'who', 'type': 'string'},), output_fields=({'name': 'text', 'type': 'string'},))\n"
    "    def greet(self, who):\n"
    "        return {'text': 'hello ' + str(who)}\n"
    "\n"
    "    @command(name='custom-name', output_fields=({'name': 'ok', 'type': 'bool'},))\n"
    "    def renamed(self):\n"
    "        return {'ok': True}\n"
    "\n"
    "    @launch_hook('prepare')\n"
    "    def tweak(self, **context):\n"
    "        return {'jvm_args': ['-Ddecorated=1']}\n"
    "\n"
    "    @event('sys-deco.ping')\n"
    "    def on_ping(self, **payload):\n"
    "        return None\n"
    "\n"
    "    def on_load(self):\n"
    "        # 显式注册覆盖装饰器同名声明，补充新命令\n"
    "        self.define_command('greet', self.explicit_greet,"
    " input_fields=({'name': 'who', 'type': 'string'},), output_fields=({'name': 'text', 'type': 'string'},))\n"
    "        self.define_command('extra', lambda: {'extra': True}, output_fields=({'name': 'extra', 'type': 'bool'},))\n"
    "\n"
    "    def explicit_greet(self, who):\n"
    "        return {'text': 'explicit ' + str(who)}\n"
)


def test_decorators_register_before_on_load_and_allow_override(tmp_path: Path) -> None:
    root = tmp_path / "resources" / "resources" / "system_plugins" / "sys-deco"
    _write_plugin(root, {"name": "sys-deco", "entry_point": "main:DecoratedPlugin"}, DECORATED_BODY)
    registered_hooks: list[tuple[str, str, str | None, bool]] = []
    service = PluginWorkerService(
        tmp_path / "data",
        hook_sink=lambda name, phase, callback_ref, required: registered_hooks.append(
            (name, phase, callback_ref, required)
        ),
    )
    try:
        service.register({"name": "sys-deco", "entry_point": "main:DecoratedPlugin"}, root, "system")
        service.enable("sys-deco")
        broker = service.broker("sys-deco")

        # 装饰器命令经预置扫描登记
        assert broker.call_command("custom-name", {}) == {"ok": True}
        # on_load 内显式注册覆盖了装饰器同名命令
        assert broker.call_command("greet", {"who": "x"}) == {"text": "explicit x"}
        assert broker.call_command("extra", {}) == {"extra": True}

        # 装饰器启动钩子经预置扫描登记进宿主钩子接收方
        assert ("sys-deco", "prepare", None, False) not in registered_hooks
        assert any(hook[1] == "prepare" and hook[0] == "sys-deco" for hook in registered_hooks)
        service.stop("sys-deco")
    finally:
        service.close()
