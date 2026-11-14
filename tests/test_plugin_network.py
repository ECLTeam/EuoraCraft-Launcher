# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_network 模块的自动化测试。
#
# 公开接口：
#   - test_plugin_can_make_controlled_http_get(tmp_path) -> None
#   - test_plugin_http_rejects_non_http_scheme(tmp_path) -> None
#   - test_plugin_http_truncates_oversized_body(tmp_path) -> None
# ============================================================

import json

import pytest

from ECL.plugins.framework import PluginManager
from ECL.plugins.network import PluginHttpError
from ECL.plugins.plugin import Plugin


class _FakeHeaders(dict):
    pass


class _FakeResponse:
    def __init__(self, status_code=200, content=b"hello world", headers=None, url="https://example.com/x"):
        self.status_code = status_code
        self.content = content
        self.headers = _FakeHeaders(headers or {"content-type": "text/plain"})
        self.url = url


class _FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def _build_framework(tmp_path, client) -> PluginManager:
    data_path = tmp_path / "data"
    plugin_dir = data_path / "plugins" / "sources" / "net-demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "net-demo", "entry_point": "main:NetPlugin"}),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins.plugin import Plugin\n"
        "class NetPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n",
        encoding="utf-8",
    )
    framework = PluginManager(http_client=client)
    framework.initialize(data_path, tmp_path / "resources")
    # 直接验证宿主 SDK 的网络单元，不通过正式第三方加载路径执行旧入口。
    metadata = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    framework._plugins["net-demo"] = Plugin(framework, plugin_dir, metadata)
    return framework


def test_plugin_can_make_controlled_http_get(tmp_path) -> None:
    fake = _FakeHttpClient(_FakeResponse())
    framework = _build_framework(tmp_path, fake)
    plugin = framework.get_plugin("net-demo")

    response = plugin.http_get("https://example.com/x")
    assert response.status_code == 200
    assert response.text == "hello world"
    assert response.truncated is False
    assert fake.calls[0][0] == "GET"
    assert fake.calls[0][1] == "https://example.com/x"


def test_plugin_http_allows_cross_site_requests(tmp_path) -> None:
    fake = _FakeHttpClient(_FakeResponse())
    framework = _build_framework(tmp_path, fake)
    plugin = framework.get_plugin("net-demo")

    # 站点权限已随权限系统移除；跨站请求照常执行。
    response = plugin.http_get("https://other.example.org/x")
    assert response.status_code == 200
    assert fake.calls[0][1] == "https://other.example.org/x"


def test_plugin_http_rejects_non_http_scheme(tmp_path) -> None:
    fake = _FakeHttpClient(_FakeResponse())
    framework = _build_framework(tmp_path, fake)
    plugin = framework.get_plugin("net-demo")

    with pytest.raises(PluginHttpError):
        plugin.http_get("file:///etc/passwd")
    assert fake.calls == []


def test_plugin_http_truncates_oversized_body(tmp_path) -> None:
    huge = b"a" * (8 * 1024 * 1024 + 10)
    fake = _FakeHttpClient(_FakeResponse(content=huge))
    framework = _build_framework(tmp_path, fake)
    plugin = framework.get_plugin("net-demo")

    response = plugin.http_get("https://example.com/x")
    assert response.truncated is True
    assert len(response.text) == 8 * 1024 * 1024
