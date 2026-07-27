from __future__ import annotations

import re
from pathlib import Path

import pytest

from residential_ip_manager.domain.models import VpnNode
from residential_ip_manager.infrastructure.ovpn_config import (
    SafeOpenVpnConfigGenerator,
    build_openvpn_config,
    sanitize_openvpn_config,
)

UNTRUSTED_CONFIG = """\
client
proto tcp
remote 8.8.8.8 443
script-security 2
up "C:/Temp/evil.exe"
--down "C:/Temp/evil.exe"
plugin malicious.dll
route-up "C:/Temp/evil.exe"
tls-verify "C:/Temp/evil.exe"
auth-user-pass old-auth.txt
socks-proxy 10.0.0.1 1080
management 0.0.0.0 7505
block-outside-dns
register-dns
<auth-user-pass>
stolen-user
stolen-password
</auth-user-pass>
<ca>
certificate-data
</ca>
"""


def _active_directives(config: str) -> list[str]:
    directives: list[str] = []
    for line in config.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "<")):
            continue
        match = re.match(r"(?:--)?([A-Za-z0-9_-]+)", stripped)
        if match:
            directives.append(match.group(1).lower())
    return directives


def _node(*, protocol: str = "tcp") -> VpnNode:
    return VpnNode(
        id="JP unsafe/name 8.8.8.8",
        ip="8.8.8.8",
        remote_host="8.8.8.8",
        remote_port=443,
        protocol=protocol,
        country_code="JP",
        country="Japan",
        config_text=UNTRUSTED_CONFIG,
    )


def test_sanitizer_removes_code_execution_and_owned_directives() -> None:
    sanitized = sanitize_openvpn_config(UNTRUSTED_CONFIG)
    directives = set(_active_directives(sanitized))

    assert not directives.intersection(
        {
            "script-security",
            "up",
            "down",
            "plugin",
            "route-up",
            "tls-verify",
            "auth-user-pass",
            "socks-proxy",
            "management",
            "block-outside-dns",
            "register-dns",
        }
    )
    assert "certificate-data" in sanitized
    assert "stolen-user" not in sanitized


def test_builder_adds_exactly_one_local_proxy_auth_and_public_bypass(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.txt"
    rendered = build_openvpn_config(
        UNTRUSTED_CONFIG,
        auth_path=auth_path,
        bypass_ips=["1.1.1.1", "1.1.1.1"],
    )
    directives = _active_directives(rendered)

    assert directives.count("socks-proxy") == 1
    assert directives.count("socks-proxy-retry") == 1
    assert directives.count("auth-user-pass") == 1
    assert directives.count("block-outside-dns") == 1
    assert directives.count("register-dns") == 1
    assert "socks-proxy 127.0.0.1 7890" in rendered
    assert 'auth-user-pass "' in rendered
    assert "# Windows DNS leak protection" in rendered
    assert rendered.count("route 1.1.1.1 255.255.255.255 net_gateway") == 1


def test_generator_writes_vpn_credentials_and_sanitized_profile(tmp_path: Path) -> None:
    output = tmp_path / "profiles"
    profile = SafeOpenVpnConfigGenerator().generate(_node(), output)

    assert profile.name == "JP_unsafe_name_8.8.8.8.ovpn"
    assert (output / "auth.txt").read_text(encoding="utf-8") == "vpn\nvpn\n"
    rendered = profile.read_text(encoding="utf-8")
    assert "socks-proxy 127.0.0.1 7890" in rendered
    assert "malicious.dll" not in rendered


def test_generator_rejects_udp_and_non_loopback_proxy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        SafeOpenVpnConfigGenerator(proxy_host="8.8.8.8")
    with pytest.raises(ValueError, match="TCP"):
        SafeOpenVpnConfigGenerator().generate(_node(protocol="udp"), tmp_path)
