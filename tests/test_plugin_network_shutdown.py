from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_pending_system_dns_does_not_create_unbounded_interpreter_exit_join(tmp_path):
    source = """
import sys
from pathlib import Path
from threading import Event
from time import monotonic
sys.path.insert(0, str(Path.cwd() / 'tests'))
from plugin_network_helpers import make_runtime
from ECL.plugin_api.contracts import ProtocolError

def resolve(*_):
    Event().wait()

runtime, _ = make_runtime(Path(sys.argv[1]), ['http:p10.example.test:80'], resolver=resolve)
try:
    runtime.handle({'method':'GET','url':'http://p10.example.test/'}, monotonic()+0.1)
except ProtocolError:
    pass
else:
    raise AssertionError('DNS did not time out')
runtime.close()
assert not runtime.is_idle()
assert not runtime.service.close()
print('pending observable; exit bounded')
"""
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "pending observable; exit bounded" in result.stdout
