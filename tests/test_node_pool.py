from datetime import UTC, datetime, timedelta

import pytest

from residential_ip_manager.application.node_pool import NodePool
from residential_ip_manager.domain.models import NodeStatus, PurityGrade, VpnNode


def make_node(
    node_id: str,
    *,
    country: str = "JP",
    grade: PurityGrade = PurityGrade.STRICT_HOME,
    status: NodeStatus = NodeStatus.AVAILABLE,
    score: int = 50,
    purity_score: int = 90,
    latency_ms: int = 50,
) -> VpnNode:
    return VpnNode(
        id=node_id,
        ip=f"198.51.100.{node_id[-1]}",
        remote_host=f"198.51.100.{node_id[-1]}",
        remote_port=443,
        protocol="tcp",
        country_code=country,
        country=country,
        purity_grade=grade,
        status=status,
        score=score,
        purity_score=purity_score,
        latency_ms=latency_ms,
    )


@pytest.mark.asyncio
async def test_refresh_is_fetch_classify_probe_and_never_probes_idc() -> None:
    strict = make_node("node-1")
    rejected = make_node("node-2", grade=PurityGrade.REJECTED)
    calls: list[str] = []

    class Source:
        async def fetch(self) -> list[VpnNode]:
            calls.append("fetch")
            return [strict, rejected]

    class Classifier:
        async def classify(self, nodes: list[VpnNode]) -> list[VpnNode]:
            calls.append("classify")
            return nodes

    class Probe:
        async def probe(self, nodes: list[VpnNode]) -> list[VpnNode]:
            calls.append("probe")
            assert nodes == [strict]
            return [strict, rejected]

    pool = NodePool(strict_home_only=True)
    result = await pool.refresh(Source(), Classifier(), Probe())

    assert calls == ["fetch", "classify", "probe"]
    assert result == [strict]
    assert pool.select() is strict


def test_selection_prefers_country_then_quality() -> None:
    us = make_node("node-1", country="US", purity_score=100, score=100)
    jp_slow = make_node("node-2", country="JP", purity_score=90, latency_ms=100)
    jp_fast = make_node("node-3", country="JP", purity_score=90, latency_ms=20)
    pool = NodePool()
    pool.replace([us, jp_slow, jp_fast])

    assert pool.select(preferred_country="JP") is jp_fast
    assert pool.select() is us


def test_failed_node_is_unavailable_until_cooldown_expires() -> None:
    node = make_node("node-1")
    pool = NodePool(cooldown_seconds=60)
    pool.replace([node])
    now = datetime(2026, 7, 27, tzinfo=UTC)

    until = pool.mark_failure(node.id, "timeout", now=now)

    assert until == now + timedelta(seconds=60)
    assert pool.select(now=now + timedelta(seconds=59)) is None
    assert node.status is NodeStatus.COOLDOWN
    assert pool.select(now=now + timedelta(seconds=61)) is node
    assert node.status is NodeStatus.AVAILABLE


def test_restore_unlocks_an_expired_persisted_cooldown() -> None:
    node = make_node("node-1", status=NodeStatus.COOLDOWN)
    pool = NodePool()
    pool.replace([node])

    pool.restore_cooldowns({node.id: datetime.now(UTC) - timedelta(seconds=1)})

    assert node.status is NodeStatus.AVAILABLE
    assert pool.select() is node
