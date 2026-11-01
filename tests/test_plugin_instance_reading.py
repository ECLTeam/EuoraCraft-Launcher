from __future__ import annotations

import ctypes
import os
import sys
from types import SimpleNamespace

import pytest

from ECL.services.game.instance_profile_transactions import NativeProfileTarget
from ECL.utils import instance_reading
from ECL.utils.file_handles import FileDirectory
from ECL.utils.instance_reading import InstanceReadBoundary, InstanceReadError, directory_identity


def test_directory_authorization_matches_opened_target_after_content_change(tmp_path):
    identity = directory_identity(tmp_path)
    (tmp_path / "new-file").write_bytes(b"content")
    assert directory_identity(tmp_path) == identity
    directory = FileDirectory.open(tmp_path)
    try:
        assert NativeProfileTarget.identity(directory) == identity
    finally:
        directory.close()


@pytest.mark.parametrize("result", [0, -1])
def test_macos_opened_path_declares_variadic_fixed_arguments(monkeypatch, tmp_path, result):
    content = os.fsencode(tmp_path) + b"\0"

    class Fcntl:
        argtypes = None
        restype = None

        def __call__(self, descriptor, operation, destination):
            assert self.argtypes == [ctypes.c_int, ctypes.c_int]
            assert self.restype is ctypes.c_int
            assert (descriptor, operation) == (42, 50)
            assert isinstance(destination, ctypes.c_void_p)
            ctypes.memmove(destination, content, len(content))
            return result

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(instance_reading.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(fcntl=Fcntl()))
    if result:
        with pytest.raises(InstanceReadError):
            InstanceReadBoundary._opened_path(42)
    else:
        assert InstanceReadBoundary._opened_path(42) == tmp_path
