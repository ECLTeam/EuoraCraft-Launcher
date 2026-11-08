from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from random import Random

from PIL import Image

from ECL.plugin_api import HostApiError
from external_plugins.ecl_onlinegame.onlinegame.extensions import ConnectorExtensionRegistry, ConnectorProtocolRequest
from external_plugins.ecl_onlinegame.onlinegame.remote_extensions import RemoteExtensions
from tests.test_plugin_qomicex_compat import make_plugin


def test_optional_protocol_transfers_large_png_and_retires_disabled_provider() -> None:
    compatible = make_plugin([])
    frames = []

    class Caller:
        enabled = True

        def list_plugin_commands(self):
            return [{"plugin": "Qomicex-Compatible", "command": "onlinegame.extension.v1"}]

        def call_plugin_command(self, _owner, _command, args, **_kwargs):
            if not self.enabled:
                raise HostApiError("目标已禁用", "PLUGIN_TARGET_DISABLED")
            frames.append(len(json.dumps(args, ensure_ascii=False).encode()))
            return compatible.extension(args["payload"])

    caller = Caller()
    registry = ConnectorExtensionRegistry()
    remote = RemoteExtensions(caller, registry)
    remote.prepare()
    image = Image.frombytes("RGBA", (128, 128), Random(1).randbytes(128 * 128 * 4))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    request = ConnectorProtocolRequest(
        "qml:player_icons",
        json.dumps({"machineId": "actual", "iconBase64": encoded}).encode(),
        peer_machine_id="actual",
    )
    status, body = asyncio.run(registry.dispatch(request))
    assert status == 0
    assert json.loads(body)["icons"]["actual"] == encoded
    assert max(frames) < 16384
    caller.enabled = False
    status, _body = asyncio.run(registry.dispatch(request))
    assert status == 255
    assert registry.protocol_names() == []
