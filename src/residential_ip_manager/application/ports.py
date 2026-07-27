from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol

from residential_ip_manager.domain.models import (
    ConnectionSnapshot,
    EnvironmentReport,
    VpnNode,
)

SnapshotListener = Callable[[ConnectionSnapshot], None]
NodeListener = Callable[[Sequence[VpnNode]], None]
LogListener = Callable[[str, str], None]


class NodeSource(Protocol):
    async def fetch(self) -> list[VpnNode]: ...


class ResidentialClassifier(Protocol):
    async def classify(self, nodes: Sequence[VpnNode]) -> list[VpnNode]: ...


class NodeProbe(Protocol):
    async def probe(self, nodes: Sequence[VpnNode]) -> list[VpnNode]: ...


class ClashController(Protocol):
    async def check_environment(self) -> EnvironmentReport: ...

    async def ensure_running(self) -> None: ...

    async def proxy_exit_ip(self) -> str: ...


class TunnelController(Protocol):
    async def check_environment(self) -> EnvironmentReport: ...

    async def repair_environment(self) -> bool: ...

    async def connect(self, node: VpnNode, config_path: Path) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_connected(self) -> bool: ...


class SystemNetworkController(Protocol):
    async def check_environment(self) -> EnvironmentReport: ...

    async def prepare(self) -> None: ...

    async def restore(self) -> None: ...

    async def public_ip(self) -> str: ...


class Sleep(Protocol):
    def __call__(self, delay: float) -> Awaitable[None]: ...
