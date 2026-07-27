from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from residential_ip_manager.domain.models import (
    ConnectionSnapshot,
    ConnectionState,
    NodeStatus,
    PurityGrade,
    ResidentialEvidence,
    VpnNode,
)


class SQLiteStore:
    """Async facade over short-lived SQLite connections.

    Connections are opened inside worker threads, so no SQLite object crosses a
    thread boundary. WAL keeps UI reads responsive while node batches are saved.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def close(self) -> None:
        # Connections are intentionally scoped to individual operations.
        return None

    async def upsert_nodes(self, nodes: Sequence[VpnNode]) -> None:
        await self.initialize()
        if nodes:
            await asyncio.to_thread(self._upsert_nodes_sync, list(nodes))

    save_nodes = upsert_nodes

    async def load_nodes(self) -> list[VpnNode]:
        await self.initialize()
        return await asyncio.to_thread(self._load_nodes_sync)

    async def save_snapshot(self, snapshot: ConnectionSnapshot) -> None:
        await self.initialize()
        await asyncio.to_thread(self._save_snapshot_sync, snapshot)

    save_connection_snapshot = save_snapshot

    async def load_snapshot(self) -> ConnectionSnapshot | None:
        await self.initialize()
        return await asyncio.to_thread(self._load_snapshot_sync)

    load_connection_snapshot = load_snapshot

    async def load_connection_history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._load_history_sync, max(0, limit))

    async def save_cooldown(self, node_id: str, until: datetime | None) -> None:
        await self.initialize()
        await asyncio.to_thread(self._save_cooldown_sync, node_id, until)

    async def load_cooldowns(self) -> dict[str, datetime]:
        await self.initialize()
        return await asyncio.to_thread(self._load_cooldowns_sync)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_version(version)
                SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);

                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    ip TEXT NOT NULL,
                    remote_host TEXT NOT NULL,
                    remote_port INTEGER NOT NULL,
                    protocol TEXT NOT NULL,
                    country_code TEXT NOT NULL,
                    country TEXT NOT NULL,
                    config_text TEXT NOT NULL,
                    city TEXT NOT NULL,
                    isp TEXT NOT NULL,
                    asn TEXT NOT NULL,
                    reverse_dns TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    advertised_ping_ms INTEGER NOT NULL,
                    speed_bps INTEGER NOT NULL,
                    sessions INTEGER NOT NULL,
                    latency_ms INTEGER,
                    purity_grade TEXT NOT NULL,
                    purity_score INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_error TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    cooldown_until TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_nodes_country_status
                    ON nodes(country_code, status);
                CREATE INDEX IF NOT EXISTS idx_nodes_purity
                    ON nodes(purity_grade, purity_score DESC);

                CREATE TABLE IF NOT EXISTS connection_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    state TEXT NOT NULL,
                    message TEXT NOT NULL,
                    active_node_id TEXT,
                    exit_ip TEXT NOT NULL,
                    clash_exit_ip TEXT NOT NULL,
                    connected_since TEXT,
                    consecutive_failures INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS connection_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    state TEXT NOT NULL,
                    message TEXT NOT NULL,
                    active_node_id TEXT,
                    exit_ip TEXT NOT NULL,
                    clash_exit_ip TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_connection_history_time
                    ON connection_history(occurred_at DESC);
                """
            )

    def _upsert_nodes_sync(self, nodes: list[VpnNode]) -> None:
        now = self._to_text(datetime.now(UTC))
        assert now is not None
        rows = [self._node_to_row(node, now) for node in nodes]
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT INTO nodes (
                    id, ip, remote_host, remote_port, protocol, country_code, country,
                    config_text, city, isp, asn, reverse_dns, score, advertised_ping_ms,
                    speed_bps, sessions, latency_ms, purity_grade, purity_score, status,
                    evidence_json, last_checked_at, last_error, updated_at, cooldown_until
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, COALESCE((SELECT cooldown_until FROM nodes WHERE id = ?), NULL)
                )
                ON CONFLICT(id) DO UPDATE SET
                    ip = excluded.ip,
                    remote_host = excluded.remote_host,
                    remote_port = excluded.remote_port,
                    protocol = excluded.protocol,
                    country_code = excluded.country_code,
                    country = excluded.country,
                    config_text = excluded.config_text,
                    city = excluded.city,
                    isp = excluded.isp,
                    asn = excluded.asn,
                    reverse_dns = excluded.reverse_dns,
                    score = excluded.score,
                    advertised_ping_ms = excluded.advertised_ping_ms,
                    speed_bps = excluded.speed_bps,
                    sessions = excluded.sessions,
                    latency_ms = excluded.latency_ms,
                    purity_grade = excluded.purity_grade,
                    purity_score = excluded.purity_score,
                    status = excluded.status,
                    evidence_json = excluded.evidence_json,
                    last_checked_at = excluded.last_checked_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    def _load_nodes_sync(self) -> list[VpnNode]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM nodes ORDER BY country_code, purity_score DESC, score DESC, id"
            ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def _save_snapshot_sync(self, snapshot: ConnectionSnapshot) -> None:
        now = self._to_text(datetime.now(UTC))
        values = (
            snapshot.state.value,
            snapshot.message,
            snapshot.active_node_id,
            snapshot.exit_ip,
            snapshot.clash_exit_ip,
            self._to_text(snapshot.connected_since),
            snapshot.consecutive_failures,
            self._json_dumps(snapshot.metadata),
            now,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO connection_state (
                    singleton_id, state, message, active_node_id, exit_ip, clash_exit_ip,
                    connected_since, consecutive_failures, metadata_json, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    state = excluded.state,
                    message = excluded.message,
                    active_node_id = excluded.active_node_id,
                    exit_ip = excluded.exit_ip,
                    clash_exit_ip = excluded.clash_exit_ip,
                    connected_since = excluded.connected_since,
                    consecutive_failures = excluded.consecutive_failures,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            connection.execute(
                """
                INSERT INTO connection_history (
                    state, message, active_node_id, exit_ip, clash_exit_ip,
                    consecutive_failures, metadata_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.state.value,
                    snapshot.message,
                    snapshot.active_node_id,
                    snapshot.exit_ip,
                    snapshot.clash_exit_ip,
                    snapshot.consecutive_failures,
                    self._json_dumps(snapshot.metadata),
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM connection_history
                WHERE id <= COALESCE((SELECT MAX(id) - 10000 FROM connection_history), 0)
                """
            )

    def _load_snapshot_sync(self) -> ConnectionSnapshot | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM connection_state WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            return None
        return ConnectionSnapshot(
            state=self._enum_or_default(ConnectionState, row["state"], ConnectionState.ERROR),
            message=row["message"],
            active_node_id=row["active_node_id"],
            exit_ip=row["exit_ip"],
            clash_exit_ip=row["clash_exit_ip"],
            connected_since=self._from_text(row["connected_since"]),
            consecutive_failures=row["consecutive_failures"],
            metadata=self._json_loads(row["metadata_json"], {}),
        )

    def _load_history_sync(self, limit: int) -> list[dict[str, Any]]:
        if limit == 0:
            return []
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT state, message, active_node_id, exit_ip, clash_exit_ip,
                       consecutive_failures, metadata_json, occurred_at
                FROM connection_history ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "state": self._enum_or_default(
                    ConnectionState, row["state"], ConnectionState.ERROR
                ),
                "message": row["message"],
                "active_node_id": row["active_node_id"],
                "exit_ip": row["exit_ip"],
                "clash_exit_ip": row["clash_exit_ip"],
                "consecutive_failures": row["consecutive_failures"],
                "metadata": self._json_loads(row["metadata_json"], {}),
                "occurred_at": self._from_text(row["occurred_at"]),
            }
            for row in rows
        ]

    def _save_cooldown_sync(self, node_id: str, until: datetime | None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE nodes SET cooldown_until = ? WHERE id = ?",
                (self._to_text(until), node_id),
            )

    def _load_cooldowns_sync(self) -> dict[str, datetime]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT id, cooldown_until FROM nodes WHERE cooldown_until IS NOT NULL"
            ).fetchall()
        cooldowns: dict[str, datetime] = {}
        for row in rows:
            parsed = self._from_text(row["cooldown_until"])
            if parsed is not None:
                cooldowns[row["id"]] = parsed
        return cooldowns

    @classmethod
    def _node_to_row(cls, node: VpnNode, updated_at: str) -> tuple[Any, ...]:
        evidence = [
            {
                "provider": item.provider,
                "passed": item.passed,
                "summary": item.summary,
                "checked_at": cls._to_text(item.checked_at),
            }
            for item in node.evidence
        ]
        return (
            node.id,
            node.ip,
            node.remote_host,
            node.remote_port,
            node.protocol,
            node.country_code,
            node.country,
            node.config_text,
            node.city,
            node.isp,
            node.asn,
            node.reverse_dns,
            node.score,
            node.advertised_ping_ms,
            node.speed_bps,
            node.sessions,
            node.latency_ms,
            node.purity_grade.value,
            node.purity_score,
            node.status.value,
            cls._json_dumps(evidence),
            cls._to_text(node.last_checked_at),
            node.last_error,
            updated_at,
            node.id,
        )

    @classmethod
    def _row_to_node(cls, row: sqlite3.Row) -> VpnNode:
        evidence_data = cls._json_loads(row["evidence_json"], [])
        evidence = [
            ResidentialEvidence(
                provider=item.get("provider", ""),
                passed=bool(item.get("passed", False)),
                summary=item.get("summary", ""),
                checked_at=cls._from_text(item.get("checked_at")) or datetime.now(UTC),
            )
            for item in evidence_data
            if isinstance(item, dict)
        ]
        return VpnNode(
            id=row["id"],
            ip=row["ip"],
            remote_host=row["remote_host"],
            remote_port=row["remote_port"],
            protocol=row["protocol"],
            country_code=row["country_code"],
            country=row["country"],
            config_text=row["config_text"],
            city=row["city"],
            isp=row["isp"],
            asn=row["asn"],
            reverse_dns=row["reverse_dns"],
            score=row["score"],
            advertised_ping_ms=row["advertised_ping_ms"],
            speed_bps=row["speed_bps"],
            sessions=row["sessions"],
            latency_ms=row["latency_ms"],
            purity_grade=cls._enum_or_default(
                PurityGrade, row["purity_grade"], PurityGrade.REJECTED
            ),
            purity_score=row["purity_score"],
            status=cls._enum_or_default(NodeStatus, row["status"], NodeStatus.UNKNOWN),
            evidence=evidence,
            last_checked_at=cls._from_text(row["last_checked_at"]),
            last_error=row["last_error"],
        )

    @staticmethod
    def _to_text(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _from_text(value: str | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _json_loads(value: str, default: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _enum_or_default(enum_type: Any, value: str, default: Any) -> Any:
        try:
            return enum_type(value)
        except ValueError:
            return default
