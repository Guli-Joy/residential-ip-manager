from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path

from residential_ip_manager.domain.models import VpnNode

_DIRECTIVE = re.compile(r"^\s*(?:--)?(?P<name>[A-Za-z0-9_-]+)(?:\s|$)")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]+")

# These options execute local programs or load code supplied by an untrusted profile.
_EXECUTION_DIRECTIVES = frozenset(
    {
        "auth-user-pass-verify",
        "client-connect",
        "client-disconnect",
        "down",
        "engine",
        "ipchange",
        "iproute",
        "learn-address",
        "plugin",
        "providers",
        "pkcs11-providers",
        "route-pre-down",
        "route-up",
        "script-security",
        "tls-crypt-v2-verify",
        "tls-verify",
        "up",
    }
)

# The application owns its proxy, credentials, and management channel.
_REPLACED_OR_UNTRUSTED_DIRECTIVES = frozenset(
    {
        "auth-user-pass",
        "block-outside-dns",
        "http-proxy",
        "http-proxy-retry",
        "http-proxy-user-pass",
        "management",
        "management-client",
        "management-client-auth",
        "management-client-group",
        "management-client-pf",
        "management-client-user",
        "management-external-cert",
        "management-external-key",
        "management-hold",
        "management-log-cache",
        "management-query-passwords",
        "management-signal",
        "management-up-down",
        "socks-proxy",
        "socks-proxy-retry",
        "register-dns",
    }
)

_REMOVED_INLINE_BLOCKS = frozenset({"auth-user-pass", "http-proxy-user-pass"})


def _directive_name(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";", "<")):
        return ""
    match = _DIRECTIVE.match(stripped)
    return match.group("name").lower() if match else ""


def sanitize_openvpn_config(config_text: str) -> str:
    """Remove local-code hooks and settings owned by this application."""

    lines: list[str] = []
    skipped_block = ""
    skip_continuation = False
    normalized = config_text.replace("\r\n", "\n").replace("\r", "\n")
    for raw_line in normalized.split("\n"):
        stripped = raw_line.strip()
        lower = stripped.lower()

        if skipped_block:
            if lower == f"</{skipped_block}>":
                skipped_block = ""
            continue
        if lower.startswith("<") and lower.endswith(">") and not lower.startswith("</"):
            block_name = lower[1:-1].strip().split()[0]
            if block_name in _REMOVED_INLINE_BLOCKS:
                skipped_block = block_name
                continue

        if skip_continuation:
            skip_continuation = raw_line.rstrip().endswith("\\")
            continue

        directive = _directive_name(raw_line)
        if directive in _EXECUTION_DIRECTIVES | _REPLACED_OR_UNTRUSTED_DIRECTIVES:
            skip_continuation = raw_line.rstrip().endswith("\\")
            continue
        lines.append(raw_line.rstrip())

    while lines and not lines[-1]:
        lines.pop()
    if not any(line.strip() and not line.lstrip().startswith(("#", ";")) for line in lines):
        raise ValueError("OpenVPN config is empty after sanitization")
    return "\n".join(lines) + "\n"


def _validate_local_proxy(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("proxy_host must be a loopback IP address") from exc
    if not address.is_loopback:
        raise ValueError("proxy_host must be a loopback IP address")
    if not 1 <= port <= 65535:
        raise ValueError("proxy_port must be between 1 and 65535")
    return str(address)


def _validated_bypass_ips(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ValueError(f"bypass IP must be public: {value}")
        normalized = str(address)
        if normalized not in result:
            result.append(normalized)
    return result


def _openvpn_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace('"', '\\"')


def build_openvpn_config(
    config_text: str,
    *,
    auth_path: Path,
    proxy_host: str = "127.0.0.1",
    proxy_port: int = 7890,
    bypass_ips: Iterable[str] = (),
) -> str:
    proxy_ip = _validate_local_proxy(proxy_host, proxy_port)
    routes = _validated_bypass_ips(bypass_ips)
    cleaned = sanitize_openvpn_config(config_text)

    additions = [
        "",
        "# Managed local Clash relay",
        f"socks-proxy {proxy_ip} {proxy_port}",
        "socks-proxy-retry",
        f'auth-user-pass "{_openvpn_path(auth_path)}"',
        "auth-nocache",
        "",
        "# Windows DNS leak protection",
        "block-outside-dns",
        "register-dns",
    ]
    for address in routes:
        if ":" in address:
            additions.append(f"route-ipv6 {address}/128 net_gateway")
        else:
            additions.append(f"route {address} 255.255.255.255 net_gateway")
    return cleaned + "\n".join(additions) + "\n"


class SafeOpenVpnConfigGenerator:
    def __init__(
        self,
        *,
        proxy_host: str = "127.0.0.1",
        proxy_port: int = 7890,
        username: str = "vpn",
        password: str = "vpn",
    ) -> None:
        self._proxy_host = _validate_local_proxy(proxy_host, proxy_port)
        self._proxy_port = proxy_port
        if "\n" in username or "\r" in username or "\n" in password or "\r" in password:
            raise ValueError("OpenVPN credentials must be single-line values")
        self._username = username
        self._password = password

    def generate(
        self,
        node: VpnNode,
        output_dir: Path,
        *,
        bypass_ips: Iterable[str] = (),
    ) -> Path:
        if node.protocol.lower() != "tcp":
            raise ValueError("Clash SOCKS relay only supports TCP OpenVPN profiles")
        if not node.config_text.strip():
            raise ValueError("node does not contain an OpenVPN config")

        output_dir.mkdir(parents=True, exist_ok=True)
        auth_path = output_dir / "auth.txt"
        self._atomic_write(auth_path, f"{self._username}\n{self._password}\n")
        with suppress(OSError):
            auth_path.chmod(0o600)

        filename = _SAFE_FILENAME.sub("_", node.id).strip("_") or "node"
        config_path = output_dir / f"{filename}.ovpn"
        rendered = build_openvpn_config(
            node.config_text,
            auth_path=auth_path,
            proxy_host=self._proxy_host,
            proxy_port=self._proxy_port,
            bypass_ips=bypass_ips,
        )
        self._atomic_write(config_path, rendered)
        return config_path

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
