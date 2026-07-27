import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from residential_ip_manager.domain.models import (
    ConnectionSnapshot,
    ConnectionState,
    NodeStatus,
    PurityGrade,
    ResidentialEvidence,
    VpnNode,
)
from residential_ip_manager.storage.sqlite_store import SQLiteStore


def make_node() -> VpnNode:
    checked_at = datetime(2026, 7, 27, 1, 2, 3, tzinfo=UTC)
    return VpnNode(
        id="jp-1",
        ip="198.51.100.10",
        remote_host="vpn.example.test",
        remote_port=443,
        protocol="tcp",
        country_code="JP",
        country="Japan",
        config_text="client\nremote vpn.example.test 443\n",
        city="Tokyo",
        isp="Example Fiber",
        asn="AS64500",
        reverse_dns="fiber.example.test",
        score=88,
        advertised_ping_ms=12,
        speed_bps=10_000_000,
        sessions=5,
        latency_ms=28,
        purity_grade=PurityGrade.STRICT_HOME,
        purity_score=97,
        status=NodeStatus.AVAILABLE,
        evidence=[ResidentialEvidence("rdns", True, "FTTH", checked_at)],
        last_checked_at=checked_at,
    )


@pytest.mark.asyncio
async def test_node_batch_and_cooldown_round_trip(tmp_path) -> None:
    database = tmp_path / "state" / "manager.db"
    store = SQLiteStore(database)
    node = make_node()
    cooldown_until = datetime.now(UTC) + timedelta(minutes=10)

    await store.upsert_nodes([node])
    await store.save_cooldown(node.id, cooldown_until)
    loaded = await store.load_nodes()
    cooldowns = await store.load_cooldowns()

    assert loaded == [node]
    assert cooldowns[node.id] == cooldown_until
    with sqlite3.connect(database) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal_mode.casefold() == "wal"


@pytest.mark.asyncio
async def test_snapshot_and_history_are_recoverable(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "manager.db")
    connected_since = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    snapshot = ConnectionSnapshot(
        state=ConnectionState.CONNECTED,
        message="connected",
        active_node_id="jp-1",
        exit_ip="198.51.100.10",
        clash_exit_ip="203.0.113.5",
        connected_since=connected_since,
        consecutive_failures=0,
        metadata={"task_id": "task-1"},
    )

    await store.save_snapshot(snapshot)
    recovered = await store.load_snapshot()
    history = await store.load_connection_history()

    assert recovered == snapshot
    assert history[0]["state"] is ConnectionState.CONNECTED
    assert history[0]["active_node_id"] == "jp-1"
    assert history[0]["metadata"] == {"task_id": "task-1"}
