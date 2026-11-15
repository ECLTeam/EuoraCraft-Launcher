from __future__ import annotations

import pytest

from ECL.plugins.network_targets import NetworkOrigin


@pytest.mark.parametrize(
    "value",
    [
        "https:api.example.com:443",
        "http:a-b.example:65535",
        "https:xn--bcher-kva.example:1",
    ],
)
def test_exact_origin_reference(value):
    assert NetworkOrigin.parse(value).reference == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://api.example.com",
        "https:api.example.com",
        "https:api.example.com:0443",
        "https:api.example.com:0",
        "https:api.example.com:65536",
        "https:api.example.com:-1",
        "https:api.example.com.:443",
        "https:*.example.com:443",
        "https:API.example.com:443",
        "https:127.0.0.1:443",
        "https:127.1:443",
        "https:localhost:443",
        "https:api_.example.com:443",
        "https:api..example.com:443",
        "https:-api.example.com:443",
        "https:api-.example.com:443",
        "https:api.example.com:443/x",
        "ftp:api.example.com:21",
        "https:api.example.com:４４３",
        "https:[2606:4700:4700::1111]:443",
        "https:xn--.example.com:443",
        "https:bücher.example:443",
    ],
)
def test_invalid_origin_reference(value):
    with pytest.raises(ValueError):
        NetworkOrigin.parse(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://[::1]/",
        "http://a.example:0/",
        "http://a.example:65536/",
        "http://a.example:bad/",
        "http://user:pass@a.example/",
        "http://a.example/#",
        "http://a.example/#frag",
        "http://a.example\\@b.example/",
        "http://a.example/%0a\n",
        "http://%61.example/",
        "http://a.example./",
        "ftp://a.example/",
        "http://a.example/ ",
        "http://a.example/" + "x" * 4096,
    ],
)
def test_invalid_url(value):
    with pytest.raises(ValueError):
        NetworkOrigin.from_url(value)


def test_url_normalization_does_not_expand_scope():
    origin, url = NetworkOrigin.from_url("HTTPS://BÜCHER.example:443/a?q=1")
    assert origin.reference == "https:xn--bcher-kva.example:443"
    assert url == "https://xn--bcher-kva.example/a?q=1"
    assert origin != NetworkOrigin.parse("http:xn--bcher-kva.example:443")
    assert origin != NetworkOrigin.parse("https:x.xn--bcher-kva.example:443")
