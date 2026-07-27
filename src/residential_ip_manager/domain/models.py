from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ConnectionState(StrEnum):
    IDLE = "idle"
    CHECKING_ENVIRONMENT = "checking_environment"
    STARTING_CLASH = "starting_clash"
    FETCHING_NODES = "fetching_nodes"
    PROBING_NODES = "probing_nodes"
    CONNECTING = "connecting"
    VERIFYING = "verifying"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    FAILING_OVER = "failing_over"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


class NodeStatus(StrEnum):
    UNKNOWN = "unknown"
    CHECKING = "checking"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    COOLDOWN = "cooldown"


class PurityGrade(StrEnum):
    REJECTED = "rejected"
    CANDIDATE = "candidate"
    STRICT_HOME = "strict_home"


@dataclass(slots=True)
class ResidentialEvidence:
    provider: str
    passed: bool
    summary: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class VpnNode:
    id: str
    ip: str
    remote_host: str
    remote_port: int
    protocol: str
    country_code: str
    country: str
    config_text: str = ""
    city: str = ""
    isp: str = ""
    asn: str = ""
    reverse_dns: str = ""
    score: int = 0
    advertised_ping_ms: int = 0
    speed_bps: int = 0
    sessions: int = 0
    latency_ms: int | None = None
    purity_grade: PurityGrade = PurityGrade.CANDIDATE
    purity_score: int = 0
    status: NodeStatus = NodeStatus.UNKNOWN
    evidence: list[ResidentialEvidence] = field(default_factory=list)
    last_checked_at: datetime | None = None
    last_error: str = ""


@dataclass(slots=True)
class ComponentCheck:
    key: str
    label: str
    ok: bool
    detail: str
    required: bool = True
    repair_action: str = ""
    repair_targets: tuple[str, ...] = ()
    manual_repair_action: str = ""
    manual_repair_targets: tuple[str, ...] = ()


@dataclass(slots=True)
class EnvironmentReport:
    checks: list[ComponentCheck] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(check.ok for check in self.checks if check.required)


@dataclass(slots=True)
class ConnectionSnapshot:
    state: ConnectionState = ConnectionState.IDLE
    message: str = "准备就绪"
    active_node_id: str | None = None
    exit_ip: str = ""
    clash_exit_ip: str = ""
    connected_since: datetime | None = None
    consecutive_failures: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
