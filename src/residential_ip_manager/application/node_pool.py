from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from residential_ip_manager.application.ports import (
    NodeProbe,
    NodeSource,
    ResidentialClassifier,
)
from residential_ip_manager.domain.models import NodeStatus, PurityGrade, VpnNode


class NodePool:
    """Current VPN node inventory with strict-home and cooldown policies."""

    def __init__(
        self,
        *,
        strict_home_only: bool = True,
        cooldown_seconds: int = 600,
    ) -> None:
        self.strict_home_only = strict_home_only
        self.cooldown_seconds = max(0, cooldown_seconds)
        self._nodes: dict[str, VpnNode] = {}
        self._cooldowns: dict[str, datetime] = {}

    async def refresh(
        self,
        source: NodeSource,
        classifier: ResidentialClassifier,
        probe: NodeProbe,
    ) -> list[VpnNode]:
        """Run the complete fetch -> classify -> probe refresh pipeline."""
        fetched = self._deduplicate(await source.fetch())
        classified = self._deduplicate(await classifier.classify(fetched))
        accepted = [node for node in classified if self._passes_purity_policy(node)]
        probe_result = self._deduplicate(await probe.probe(accepted)) if accepted else []
        probed = [node for node in probe_result if self._passes_purity_policy(node)]
        self.replace(probed)
        return self.all_nodes()

    def replace(self, nodes: Sequence[VpnNode]) -> None:
        self._nodes = {node.id: node for node in self._deduplicate(nodes)}
        self._cooldowns = {
            node_id: until for node_id, until in self._cooldowns.items() if node_id in self._nodes
        }
        self._expire_cooldowns(datetime.now(UTC))
        for node_id in self._cooldowns:
            self._nodes[node_id].status = NodeStatus.COOLDOWN

    def upsert(self, nodes: Iterable[VpnNode]) -> None:
        for node in nodes:
            self._nodes[node.id] = node
        self._expire_cooldowns(datetime.now(UTC))

    def all_nodes(self) -> list[VpnNode]:
        return sorted(self._nodes.values(), key=self._display_sort_key)

    @property
    def nodes(self) -> list[VpnNode]:
        return self.all_nodes()

    def get(self, node_id: str) -> VpnNode | None:
        return self._nodes.get(node_id)

    def eligible_nodes(
        self,
        *,
        preferred_country: str = "",
        exclude_ids: Iterable[str] = (),
        now: datetime | None = None,
    ) -> list[VpnNode]:
        current_time = self._as_utc(now or datetime.now(UTC))
        self._expire_cooldowns(current_time)
        excluded = set(exclude_ids)
        candidates = [
            node
            for node in self._nodes.values()
            if node.id not in excluded and self.is_eligible(node, now=current_time)
        ]
        country = preferred_country.casefold()
        return sorted(candidates, key=lambda node: self._selection_sort_key(node, country))

    def select(
        self,
        *,
        preferred_country: str = "",
        exclude_ids: Iterable[str] = (),
        now: datetime | None = None,
    ) -> VpnNode | None:
        candidates = self.eligible_nodes(
            preferred_country=preferred_country,
            exclude_ids=exclude_ids,
            now=now,
        )
        return candidates[0] if candidates else None

    def is_eligible(self, node: VpnNode, *, now: datetime | None = None) -> bool:
        current_time = self._as_utc(now or datetime.now(UTC))
        cooldown_until = self._cooldowns.get(node.id)
        if cooldown_until is not None and cooldown_until > current_time:
            return False
        return self._passes_purity_policy(node) and node.status is NodeStatus.AVAILABLE

    def accepts(self, node: VpnNode) -> bool:
        """Return whether a node satisfies the configured residential policy."""
        return self._passes_purity_policy(node)

    def mark_failure(
        self,
        node_id: str,
        error: str,
        *,
        cooldown_seconds: int | None = None,
        now: datetime | None = None,
    ) -> datetime | None:
        node = self._nodes.get(node_id)
        if node is None:
            return None
        current_time = self._as_utc(now or datetime.now(UTC))
        duration = self.cooldown_seconds if cooldown_seconds is None else max(0, cooldown_seconds)
        cooldown_until = current_time + timedelta(seconds=duration)
        self._cooldowns[node_id] = cooldown_until
        node.status = NodeStatus.COOLDOWN
        node.last_error = error
        node.last_checked_at = current_time
        return cooldown_until

    def mark_success(self, node_id: str, *, now: datetime | None = None) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            return
        self._cooldowns.pop(node_id, None)
        node.status = NodeStatus.AVAILABLE
        node.last_error = ""
        node.last_checked_at = self._as_utc(now or datetime.now(UTC))

    def cooldown_until(self, node_id: str) -> datetime | None:
        return self._cooldowns.get(node_id)

    def cooldowns(self) -> dict[str, datetime]:
        self._expire_cooldowns(datetime.now(UTC))
        return dict(self._cooldowns)

    def restore_cooldowns(self, cooldowns: dict[str, datetime]) -> None:
        now = datetime.now(UTC)
        self._cooldowns = {
            node_id: self._as_utc(until)
            for node_id, until in cooldowns.items()
            if node_id in self._nodes and self._as_utc(until) > now
        }
        for node in self._nodes.values():
            if node.status is NodeStatus.COOLDOWN and node.id not in self._cooldowns:
                node.status = NodeStatus.AVAILABLE
        for node_id in self._cooldowns:
            self._nodes[node_id].status = NodeStatus.COOLDOWN

    def _passes_purity_policy(self, node: VpnNode) -> bool:
        if self.strict_home_only:
            return node.purity_grade is PurityGrade.STRICT_HOME
        return node.purity_grade is not PurityGrade.REJECTED

    def _expire_cooldowns(self, now: datetime) -> None:
        expired = [node_id for node_id, until in self._cooldowns.items() if until <= now]
        for node_id in expired:
            self._cooldowns.pop(node_id, None)
            node = self._nodes.get(node_id)
            if node is not None and node.status is NodeStatus.COOLDOWN:
                node.status = NodeStatus.AVAILABLE

    @staticmethod
    def _deduplicate(nodes: Sequence[VpnNode] | Iterable[VpnNode]) -> list[VpnNode]:
        unique: dict[str, VpnNode] = {}
        for node in nodes:
            unique[node.id] = node
        return list(unique.values())

    @staticmethod
    def _selection_sort_key(node: VpnNode, preferred_country: str) -> tuple[object, ...]:
        same_country = bool(preferred_country) and (
            node.country_code.casefold() == preferred_country
            or node.country.casefold() == preferred_country
        )
        latency = node.latency_ms if node.latency_ms is not None else float("inf")
        return (
            0 if same_country else 1,
            -node.purity_score,
            -node.score,
            latency,
            node.advertised_ping_ms or float("inf"),
            -node.speed_bps,
            node.id,
        )

    @staticmethod
    def _display_sort_key(node: VpnNode) -> tuple[object, ...]:
        return (node.country_code, -node.purity_score, -node.score, node.id)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
