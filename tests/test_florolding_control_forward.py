from __future__ import annotations

import importlib
from types import SimpleNamespace


def test_binding_game_port_keeps_control_port_forward(monkeypatch):
    module = importlib.import_module("ECL.services.florolding.Florolding.Florolding")
    florolding = module.Florolding()
    host = {"hostname": "scaffolding-mc-server-3939", "ipv4_addr": {"address": {"addr": 0x0A010201}}}
    monkeypatch.setattr(florolding, "get_host_node_info", lambda *_: host)
    applied = []
    node = SimpleNamespace(apply_config=applied.append)
    client = SimpleNamespace(server_port=40001, sync_c_server_port=lambda: 25565)
    assert florolding.bind_mc_port(client, node, bind_mc_port=40002, broadcast=False) == (True, None)
    forwards = applied[0]["port_forward"]
    assert {item["bind_addr"]: item["dst_addr"] for item in forwards} == {
        "127.0.0.1:40001": "10.1.2.1:3939",
        "0.0.0.0:40002": "10.1.2.1:25565",
    }
