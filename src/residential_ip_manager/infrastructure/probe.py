from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from time import perf_counter

from residential_ip_manager.domain.models import NodeStatus, VpnNode


class TcpNodeProbe:
    """Bounded asynchronous TCP reachability probe for VPNGate endpoints."""

    def __init__(self, *, timeout_seconds: float = 3.0, max_concurrency: int = 20) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self._timeout = timeout_seconds
        self._max_concurrency = max_concurrency

    async def probe(self, nodes: Sequence[VpnNode]) -> list[VpnNode]:
        result = list(nodes)
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def bounded_probe(node: VpnNode) -> None:
            async with semaphore:
                await self._probe_one(node)

        await asyncio.gather(*(bounded_probe(node) for node in result))
        return result

    async def _probe_one(self, node: VpnNode) -> None:
        node.status = NodeStatus.CHECKING
        node.last_error = ""
        node.latency_ms = None
        if node.protocol.lower() != "tcp":
            node.status = NodeStatus.UNAVAILABLE
            node.last_error = "快速探活不能可靠验证 UDP 节点"
            node.last_checked_at = datetime.now(UTC)
            return

        started = perf_counter()
        writer: asyncio.StreamWriter | None = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(node.remote_host, node.remote_port),
                timeout=self._timeout,
            )
            node.latency_ms = max(1, round((perf_counter() - started) * 1000))
            node.status = NodeStatus.AVAILABLE
        except (TimeoutError, OSError) as exc:
            node.status = NodeStatus.UNAVAILABLE
            node.last_error = str(exc) or type(exc).__name__
        finally:
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
            node.last_checked_at = datetime.now(UTC)
