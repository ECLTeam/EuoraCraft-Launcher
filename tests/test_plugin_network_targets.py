from __future__ import annotations

from ipaddress import ip_address

import pytest
from pydantic import ValidationError

from ECL.plugins.grant_models import PermissionDecision, PermissionDeclaration
from ECL.plugins.network_targets import NetworkOrigin, NetworkTargetPolicy
from ECL.plugins.runtime.models import NetworkRequest


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


@pytest.mark.parametrize(
    "values", [None, (), ("https:a.example:443",) * 2, tuple(f"https:a{i}.example:443" for i in range(17))]
)
def test_network_declaration_requires_nonempty_unique_bounded_scopes(values):
    kwargs = {} if values is None else {"resource_refs": values}
    with pytest.raises(ValidationError):
        PermissionDeclaration(capability="network.read", **kwargs)


def test_network_decision_cannot_allow_empty_or_noncanonical_scopes():
    assert PermissionDecision(capability="network.read", decision="deny").resource_refs == ()
    with pytest.raises(ValidationError):
        PermissionDecision(capability="network.read", decision="allow")
    with pytest.raises(ValidationError):
        PermissionDecision(capability="network.read", decision="deny", resource_refs=("https:a.example:0443",))


@pytest.mark.parametrize(
    "address",
    [
        "0.1.2.3",
        "10.1.1.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "172.16.0.1",
        "192.0.0.8",
        "192.0.0.9",
        "192.0.2.1",
        "192.31.196.1",
        "192.52.193.1",
        "192.88.99.2",
        "192.168.1.1",
        "192.175.48.1",
        "198.18.1.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "::ffff:8.8.8.8",
        "::8.8.8.8",
        "64:ff9b::808:808",
        "64:ff9b:1::1",
        "100::1",
        "2001::1",
        "2001:1::1",
        "2001:20::1",
        "2001:db8::1",
        "2002::1",
        "2620:4f:8000::1",
        "3fff::1",
        "4000::1",
        "5f00::1",
        "fc00::1",
        "fe80::1",
        "ff00::1",
    ],
)
def test_special_purpose_targets_are_denied(address):
    assert not NetworkTargetPolicy.allows(ip_address(address))


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2001:4860:4860::8888", "2606:4700:4700::1111"]
)
def test_ordinary_public_targets_are_allowed(address):
    assert NetworkTargetPolicy.allows(ip_address(address))


@pytest.mark.parametrize(
    "patch",
    [
        {"method": "POST"},
        {"method": "get"},
        {"url": 3},
        {"body_b64": ""},
        {"headers": {"Host": "a.example"}},
        {"headers": {"Cookie": "private"}},
        {"headers": {"Proxy-Authorization": "private"}},
        {"headers": {"Accept-Encoding": "gzip"}},
        {"headers": {"Accept": "x\r\n"}},
        {"headers": {"Accept": "\t"}},
        {"headers": {"Accept": "中文"}},
        {"headers": {"Accept": "x" * 1025}},
        {"headers": {"Accept": "x", "accept": "y"}},
        {"headers": {"Accept": 3}},
        {"headers": {"Authorization": "secret"}, "url": "http://a.example/"},
        {"headers": {"X-API-Key": "secret"}, "url": "http://a.example/"},
        {"headers": dict.fromkeys(["Accept", "If-None-Match", "If-Modified-Since", "User-Agent"], "x" * 1024)},
    ],
)
def test_strict_request_rejects_unsafe_inputs(patch):
    with pytest.raises(ValidationError):
        NetworkRequest.model_validate({"method": "GET", "url": "https://a.example/"} | patch)
