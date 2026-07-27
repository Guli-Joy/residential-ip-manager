from __future__ import annotations

import asyncio
import time

from residential_ip_manager.domain.models import NodeStatus, PurityGrade
from residential_ip_manager.infrastructure.classification import StrictResidentialClassifier
from residential_ip_manager.infrastructure.probe import TcpNodeProbe
from residential_ip_manager.infrastructure.vpngate import VpnGateNodeSource


async def run() -> None:
    started = time.perf_counter()
    nodes = await VpnGateNodeSource().fetch()
    fetched_at = time.perf_counter()
    classified = await StrictResidentialClassifier().classify(nodes)
    classified_at = time.perf_counter()
    strict = [node for node in classified if node.purity_grade is PurityGrade.STRICT_HOME]
    checked = await TcpNodeProbe(max_concurrency=32).probe(strict)
    probed_at = time.perf_counter()
    available = [node for node in checked if node.status is NodeStatus.AVAILABLE]
    elapsed = time.perf_counter() - started
    print(
        f"fetched={len(nodes)} strict={len(strict)} "
        f"available={len(available)} seconds={elapsed:.2f} "
        f"fetch={fetched_at - started:.2f} "
        f"classify={classified_at - fetched_at:.2f} "
        f"probe={probed_at - classified_at:.2f}"
    )
    for node in available:
        print(
            f"{node.country_code} {node.ip}:{node.remote_port} "
            f"{node.isp} {node.latency_ms}ms purity={node.purity_score}"
        )


if __name__ == "__main__":
    asyncio.run(run())
