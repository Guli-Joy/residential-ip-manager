from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Protocol

from residential_ip_manager.application.node_pool import NodePool
from residential_ip_manager.application.ports import (
    ClashController,
    LogListener,
    NodeListener,
    NodeProbe,
    NodeSource,
    ResidentialClassifier,
    Sleep,
    SnapshotListener,
    SystemNetworkController,
    TunnelController,
)
from residential_ip_manager.config import AppSettings
from residential_ip_manager.domain.errors import AppError, ErrorCode
from residential_ip_manager.domain.models import (
    ConnectionSnapshot,
    ConnectionState,
    EnvironmentReport,
    NodeStatus,
    PurityGrade,
    VpnNode,
)

ConfigBuilder = Callable[[VpnNode], Path | Awaitable[Path]]
_ASN_PATTERN = re.compile(r"\bAS\s*(\d+)\b", re.IGNORECASE)


class StateStore(Protocol):
    async def initialize(self) -> None: ...

    async def upsert_nodes(self, nodes: Sequence[VpnNode]) -> None: ...

    async def load_nodes(self) -> list[VpnNode]: ...

    async def save_snapshot(self, snapshot: ConnectionSnapshot) -> None: ...

    async def load_snapshot(self) -> ConnectionSnapshot | None: ...

    async def save_cooldown(self, node_id: str, until: datetime | None) -> None: ...

    async def load_cooldowns(self) -> dict[str, datetime]: ...


_ALLOWED_TRANSITIONS: dict[ConnectionState, set[ConnectionState]] = {
    ConnectionState.IDLE: {
        ConnectionState.CHECKING_ENVIRONMENT,
        ConnectionState.FETCHING_NODES,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.CHECKING_ENVIRONMENT: {
        ConnectionState.STARTING_CLASH,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.STARTING_CLASH: {
        ConnectionState.FETCHING_NODES,
        ConnectionState.CONNECTING,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.FETCHING_NODES: {
        ConnectionState.PROBING_NODES,
        ConnectionState.IDLE,
        ConnectionState.STARTING_CLASH,
        ConnectionState.CONNECTED,
        ConnectionState.DEGRADED,
        ConnectionState.FAILING_OVER,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.PROBING_NODES: {
        ConnectionState.IDLE,
        ConnectionState.STARTING_CLASH,
        ConnectionState.CONNECTING,
        ConnectionState.CONNECTED,
        ConnectionState.DEGRADED,
        ConnectionState.FAILING_OVER,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.CONNECTING: {
        ConnectionState.VERIFYING,
        ConnectionState.FAILING_OVER,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.VERIFYING: {
        ConnectionState.CONNECTED,
        ConnectionState.FAILING_OVER,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.CONNECTED: {
        ConnectionState.FETCHING_NODES,
        ConnectionState.DEGRADED,
        ConnectionState.FAILING_OVER,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.DEGRADED: {
        ConnectionState.FETCHING_NODES,
        ConnectionState.CONNECTED,
        ConnectionState.FAILING_OVER,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.FAILING_OVER: {
        ConnectionState.FETCHING_NODES,
        ConnectionState.CONNECTING,
        ConnectionState.DISCONNECTING,
        ConnectionState.ERROR,
    },
    ConnectionState.DISCONNECTING: {
        ConnectionState.IDLE,
        ConnectionState.ERROR,
    },
    ConnectionState.ERROR: {
        ConnectionState.IDLE,
        ConnectionState.CHECKING_ENVIRONMENT,
        ConnectionState.CONNECTING,
        ConnectionState.FETCHING_NODES,
        ConnectionState.FAILING_OVER,
        ConnectionState.DISCONNECTING,
    },
}


class ConnectionOrchestrator:
    """Coordinates node refresh, tunnel lifecycle, health checks, and failover."""

    def __init__(
        self,
        *,
        source: NodeSource,
        classifier: ResidentialClassifier,
        probe: NodeProbe,
        clash: ClashController,
        tunnel: TunnelController,
        network: SystemNetworkController,
        config_builder: ConfigBuilder,
        settings: AppSettings | None = None,
        node_pool: NodePool | None = None,
        store: StateStore | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings = settings or AppSettings()
        self.source = source
        self.classifier = classifier
        self.probe = probe
        self.clash = clash
        self.tunnel = tunnel
        self.network = network
        self.config_builder = config_builder
        self.node_pool = node_pool or NodePool(
            strict_home_only=self.settings.strict_home_only,
            cooldown_seconds=self.settings.cooldown_seconds,
        )
        self.store = store
        self.sleep = sleep

        self._snapshot = ConnectionSnapshot()
        self._operation_lock = asyncio.Lock()
        self._current_operation: asyncio.Task[Any] | None = None
        self._snapshot_listeners: list[SnapshotListener] = []
        self._node_listeners: list[NodeListener] = []
        self._log_listeners: list[LogListener] = []
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def snapshot(self) -> ConnectionSnapshot:
        return self._copy_snapshot()

    @property
    def operation_in_progress(self) -> bool:
        return self._operation_lock.locked()

    def add_snapshot_listener(self, listener: SnapshotListener) -> Callable[[], None]:
        self._snapshot_listeners.append(listener)
        return lambda: self._remove_listener(self._snapshot_listeners, listener)

    subscribe_snapshot = add_snapshot_listener

    def add_node_listener(self, listener: NodeListener) -> Callable[[], None]:
        self._node_listeners.append(listener)
        return lambda: self._remove_listener(self._node_listeners, listener)

    subscribe_nodes = add_node_listener

    def add_log_listener(self, listener: LogListener) -> Callable[[], None]:
        self._log_listeners.append(listener)
        return lambda: self._remove_listener(self._log_listeners, listener)

    subscribe_log = add_log_listener

    async def initialize(self) -> ConnectionSnapshot:
        async with self._operation():
            if self.store is None:
                self._emit_snapshot()
                return self.snapshot
            await self.store.initialize()
            nodes, saved_snapshot, cooldowns = await asyncio.gather(
                self.store.load_nodes(),
                self.store.load_snapshot(),
                self.store.load_cooldowns(),
            )
            self.node_pool.replace(nodes)
            self.node_pool.restore_cooldowns(cooldowns)
            if saved_snapshot is not None:
                restored = replace(
                    saved_snapshot,
                    metadata=dict(saved_snapshot.metadata),
                )
                if restored.state is not ConnectionState.IDLE:
                    restored = ConnectionSnapshot(
                        state=ConnectionState.IDLE,
                        message="已恢复节点缓存，请重新连接",
                    )
                self._snapshot = restored
            self._emit_nodes(self.node_pool.all_nodes())
            self._emit_snapshot()
            return self.snapshot

    restore = initialize

    async def refresh_nodes(self) -> list[VpnNode]:
        async with self._operation():
            return await self._refresh_nodes_locked()

    async def connect(
        self,
        node_id: str | None = None,
        *,
        country: str | None = None,
    ) -> ConnectionSnapshot:
        async with self._operation():
            try:
                await self._normalize_for_new_operation()
                if self._snapshot.state in {ConnectionState.CONNECTED, ConnectionState.DEGRADED}:
                    if node_id is None or node_id == self._snapshot.active_node_id:
                        active_node = self._active_node()
                        if active_node is not None:
                            healthy, _ = await self._probe_active_connection(active_node)
                            if healthy:
                                return self.snapshot
                    await self._disconnect_locked()

                await self._set_state(
                    ConnectionState.CHECKING_ENVIRONMENT,
                    "正在检查运行环境",
                )
                await self._prepare_environment()

                await self._set_state(ConnectionState.STARTING_CLASH, "正在启动并检查 Clash")
                try:
                    await self.clash.ensure_running()
                    clash_exit_ip = (await self.clash.proxy_exit_ip()).strip()
                except asyncio.CancelledError:
                    raise
                except AppError:
                    raise
                except Exception as error:
                    raise AppError(
                        ErrorCode.CLASH_PORT_UNAVAILABLE,
                        "Clash 中转未就绪",
                        detail=str(error),
                    ) from error
                self._snapshot.clash_exit_ip = clash_exit_ip
                self._snapshot.metadata = {"clash_running": True}

                preferred_country = country if country is not None else self.settings.country_filter
                node = self._select_node(node_id=node_id, country=preferred_country)
                if node is None:
                    await self._refresh_nodes_locked(return_state=ConnectionState.STARTING_CLASH)
                    node = self._select_node(node_id=node_id, country=preferred_country)
                if node is None:
                    raise AppError(
                        ErrorCode.NO_STRICT_HOME_NODE,
                        "没有可用的严格家庭宽带节点",
                    )

                candidates = self._connection_candidates(
                    node,
                    preferred_country=preferred_country,
                )
                last_error: AppError | None = None
                for attempt, candidate in enumerate(candidates, start=1):
                    self._emit_log(
                        "info",
                        f"连接尝试 {attempt}/{len(candidates)}：{candidate.country} {candidate.ip}",
                    )
                    try:
                        await self._connect_node_locked(candidate, failover=False)
                        return self.snapshot
                    except asyncio.CancelledError:
                        raise
                    except AppError as error:
                        last_error = error
                        if attempt >= len(candidates) or not self._is_retryable_connection_error(
                            error
                        ):
                            raise
                        self._emit_log(
                            "warning",
                            f"节点 {candidate.ip} 连接失败，正在自动尝试下一个可用节点：{error}",
                        )
                if last_error is not None:
                    raise last_error
                raise AppError(ErrorCode.NO_STRICT_HOME_NODE, "没有可尝试的家庭宽带节点")
            except asyncio.CancelledError:
                await self._cancel_connection_locked()
                raise
            except AppError as error:
                if self._snapshot.state not in {
                    ConnectionState.ERROR,
                    ConnectionState.CONNECTED,
                }:
                    await self._set_state(
                        ConnectionState.ERROR,
                        str(error),
                        active_node_id=None,
                        exit_ip="",
                        connected_since=None,
                        metadata=self._disconnected_metadata(),
                    )
                self._emit_log("error", f"{error.code.value}: {error}")
                raise
            except Exception as error:
                wrapped = AppError(
                    ErrorCode.OPENVPN_CONNECT_FAILED,
                    "连接流程执行失败",
                    detail=str(error),
                )
                if self._snapshot.state is not ConnectionState.ERROR:
                    await self._set_state(
                        ConnectionState.ERROR,
                        str(wrapped),
                        active_node_id=None,
                        exit_ip="",
                        connected_since=None,
                        metadata=self._disconnected_metadata(),
                    )
                self._emit_log("error", f"{wrapped.code.value}: {error}")
                raise wrapped from error

    async def disconnect(self) -> ConnectionSnapshot:
        async with self._operation():
            try:
                await self._disconnect_locked()
                return self.snapshot
            except asyncio.CancelledError:
                await self._cancel_connection_locked()
                raise

    async def check_health(self) -> bool:
        async with self._operation():
            try:
                if self._snapshot.state not in {
                    ConnectionState.CONNECTED,
                    ConnectionState.DEGRADED,
                }:
                    return False
                node = self._active_node()
                if node is None:
                    await self._cleanup_connection(suppress_errors=True)
                    await self._set_state(
                        ConnectionState.ERROR,
                        "当前连接节点不存在，已清理连接状态",
                        active_node_id=None,
                        exit_ip="",
                        connected_since=None,
                        metadata=self._disconnected_metadata(),
                    )
                    return False

                healthy, detail = await self._probe_active_connection(node)
                if healthy:
                    self.node_pool.mark_success(node.id)
                    await self._persist_node(node)
                    metadata = dict(self._snapshot.metadata)
                    metadata.update(
                        {
                            "clash_running": True,
                            "tunnel_connected": True,
                            "exit_verified": True,
                        }
                    )
                    verified_exit_ip = str(metadata.get("verified_exit_ip") or node.ip)
                    await self._set_state(
                        ConnectionState.CONNECTED,
                        "连接健康",
                        consecutive_failures=0,
                        exit_ip=verified_exit_ip,
                        metadata=metadata,
                    )
                    return True

                failures = self._snapshot.consecutive_failures + 1
                metadata = dict(self._snapshot.metadata)
                metadata.update(
                    {
                        "clash_running": True,
                        "tunnel_connected": detail != "OpenVPN 隧道已断开",
                        "exit_verified": False,
                    }
                )
                await self._set_state(
                    ConnectionState.DEGRADED,
                    f"连接异常（{failures}/{self.settings.failure_threshold}）：{detail}",
                    consecutive_failures=failures,
                    metadata=metadata,
                )
                if failures < self.settings.failure_threshold or not self.settings.auto_failover:
                    return False

                try:
                    await self._failover_locked(reason=detail)
                except AppError as error:
                    self._emit_log("error", f"自动切换失败: {error}")
                    return False
                return self._snapshot.state is ConnectionState.CONNECTED
            except asyncio.CancelledError:
                if self._snapshot.state in {
                    ConnectionState.CONNECTING,
                    ConnectionState.VERIFYING,
                    ConnectionState.FAILING_OVER,
                }:
                    await self._cancel_connection_locked()
                raise

    health_check_once = check_health

    async def failover(self, *, reason: str = "手动切换") -> ConnectionSnapshot:
        async with self._operation():
            try:
                await self._failover_locked(reason=reason)
                return self.snapshot
            except asyncio.CancelledError:
                await self._cancel_connection_locked()
                raise

    def cancel_current_operation(self) -> bool:
        task = self._current_operation
        if task is None or task.done() or task is asyncio.current_task():
            return False
        task.cancel()
        return True

    async def run_health_monitor(self, stop_event: asyncio.Event | None = None) -> None:
        while stop_event is None or not stop_event.is_set():
            await self.sleep(self.settings.active_health_interval_seconds)
            if stop_event is not None and stop_event.is_set():
                break
            try:
                await self.check_health()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._emit_log("error", f"健康检查异常: {error}")

    async def run_refresh_loop(self, stop_event: asyncio.Event | None = None) -> None:
        while stop_event is None or not stop_event.is_set():
            await self.sleep(self.settings.refresh_interval_seconds)
            if stop_event is not None and stop_event.is_set():
                break
            try:
                await self.refresh_nodes()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._emit_log("error", f"节点刷新异常: {error}")

    def start_background_tasks(self) -> None:
        active = {task for task in self._background_tasks if not task.done()}
        self._background_tasks = active
        if active:
            return
        self._track_background_task(asyncio.create_task(self.run_health_monitor()))
        self._track_background_task(asyncio.create_task(self.run_refresh_loop()))

    async def stop_background_tasks(self) -> None:
        tasks = list(self._background_tasks)
        self._background_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        await self.stop_background_tasks()
        await self.disconnect()
        if self.store is not None:
            close = getattr(self.store, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def _refresh_nodes_locked(
        self,
        *,
        return_state: ConnectionState | None = None,
    ) -> list[VpnNode]:
        previous_state = return_state or self._refresh_return_state()
        active_node = self._active_node()
        try:
            await self._set_state(ConnectionState.FETCHING_NODES, "正在获取 VPNGate 节点")
            fetched = await self.source.fetch()
            classified = await self.classifier.classify(fetched)
            accepted = [node for node in classified if self.node_pool.accepts(node)]
            await self._set_state(ConnectionState.PROBING_NODES, "正在检测严格家宽节点")
            probe_result = await self.probe.probe(accepted) if accepted else []
            probed = [node for node in probe_result if self.node_pool.accepts(node)]
            self.node_pool.replace(probed)
            if active_node is not None and self.node_pool.get(active_node.id) is None:
                self.node_pool.upsert([active_node])
            nodes = self.node_pool.all_nodes()
            await self._persist_nodes(nodes)
            self._emit_nodes(nodes)
            raw_countries = {node.country_code for node in fetched if node.country_code}
            strict_nodes = [
                node for node in classified if node.purity_grade is PurityGrade.STRICT_HOME
            ]
            strict_countries = {node.country_code for node in strict_nodes if node.country_code}
            available_nodes = [node for node in nodes if node.status is NodeStatus.AVAILABLE]
            transport = str(getattr(self.source, "last_transport", "") or "默认网络")
            self._emit_log(
                "info",
                f"VPNGate 获取成功（{transport}）：原始 {len(fetched)} 个/"
                f"{len(raw_countries)} 个国家，严格家宽 {len(strict_nodes)} 个/"
                f"{len(strict_countries)} 个国家，探活可用 {len(available_nodes)} 个",
            )
            await self._set_state(
                previous_state,
                f"节点刷新完成：{len(strict_countries)} 个严格家宽国家，"
                f"{len(available_nodes)} 个可用节点",
            )
            return nodes
        except asyncio.CancelledError:
            await self._set_state(previous_state, "节点刷新已取消")
            raise
        except Exception as error:
            await self._set_state(previous_state, f"节点刷新失败: {error}")
            if isinstance(error, AppError):
                raise
            raise AppError(
                ErrorCode.VPNGATE_UNAVAILABLE,
                "无法刷新 VPNGate 节点",
                detail=str(error),
            ) from error

    async def _connect_node_locked(self, node: VpnNode, *, failover: bool) -> None:
        target_on_failure = ConnectionState.FAILING_OVER if failover else ConnectionState.ERROR
        try:
            await self._set_state(
                ConnectionState.CONNECTING,
                f"正在连接 {node.country} {node.ip}",
                active_node_id=node.id,
                exit_ip="",
                metadata=self._node_metadata(node, clash_running=True),
            )
            config_path = await self._build_config(node)
            await self.network.prepare()
            await self.tunnel.connect(node, config_path)
            await self._set_state(
                ConnectionState.VERIFYING,
                "正在验证公网出口",
                metadata=self._node_metadata(
                    node,
                    clash_running=True,
                    tunnel_connected=True,
                ),
            )
            if not await self.tunnel.is_connected():
                raise AppError(ErrorCode.OPENVPN_CONNECT_FAILED, "OpenVPN 未建立有效隧道")
            exit_ip = (await self.network.public_ip()).strip()
            verified_exit, verification = await self._verify_exit(node, exit_ip)

            self.node_pool.mark_success(node.id)
            await self._persist_node(node)
            self._emit_nodes(self.node_pool.all_nodes())
            if verification == "same_asn_nat":
                self._emit_log(
                    "info",
                    f"出口验证通过（同 ASN 运营商 NAT）：节点 {node.ip} -> "
                    f"公网出口 {verified_exit.ip}，{verified_exit.asn}",
                )
            await self._set_state(
                ConnectionState.CONNECTED,
                (
                    f"已连接 {node.country} {node.ip}，NAT 出口 {verified_exit.ip}"
                    if verification == "same_asn_nat"
                    else f"已连接 {node.country} {node.ip}"
                ),
                active_node_id=node.id,
                exit_ip=verified_exit.ip,
                connected_since=datetime.now(UTC),
                consecutive_failures=0,
                metadata=self._verified_exit_metadata(
                    node,
                    verified_exit,
                    verification=verification,
                ),
            )
        except asyncio.CancelledError:
            await self._cleanup_connection(suppress_errors=True)
            raise
        except Exception as error:
            app_error = self._as_connection_error(error)
            await self._cleanup_connection(suppress_errors=True)
            cooldown_until = self.node_pool.mark_failure(
                node.id,
                str(app_error),
                cooldown_seconds=self.settings.cooldown_seconds,
            )
            await self._persist_node(node, cooldown_until=cooldown_until)
            self._emit_nodes(self.node_pool.all_nodes())
            await self._set_state(
                target_on_failure,
                str(app_error),
                active_node_id=None,
                exit_ip="",
                connected_since=None,
                metadata=self._disconnected_metadata(),
            )
            raise app_error from error

    async def _disconnect_locked(self) -> None:
        if self._snapshot.state is ConnectionState.IDLE and not self._has_connection_context():
            return
        if self._snapshot.state is not ConnectionState.DISCONNECTING:
            await self._set_state(ConnectionState.DISCONNECTING, "正在断开连接")
        errors = await self._cleanup_connection(suppress_errors=False)
        if errors:
            detail = "; ".join(str(error) for error in errors)
            app_error = AppError(
                ErrorCode.NETWORK_RESTORE_FAILED,
                "断开后未能完整恢复网络设置",
                detail=detail,
            )
            await self._set_state(ConnectionState.ERROR, str(app_error))
            raise app_error
        await self._set_state(
            ConnectionState.IDLE,
            "已断开",
            active_node_id=None,
            exit_ip="",
            connected_since=None,
            consecutive_failures=0,
            metadata=self._disconnected_metadata(),
        )

    async def _failover_locked(self, *, reason: str) -> None:
        old_node = self._active_node()
        if old_node is None:
            raise AppError(ErrorCode.OPENVPN_CONNECT_FAILED, "没有可切换的当前连接")

        await self._set_state(
            ConnectionState.FAILING_OVER,
            f"当前出口异常，准备自动切换: {reason}",
        )
        cooldown_until = self.node_pool.mark_failure(
            old_node.id,
            reason,
            cooldown_seconds=self.settings.cooldown_seconds,
        )
        await self._persist_node(old_node, cooldown_until=cooldown_until)
        await self._cleanup_connection(suppress_errors=True)
        self._snapshot.active_node_id = None
        self._snapshot.exit_ip = ""
        self._snapshot.connected_since = None
        self._snapshot.metadata = self._disconnected_metadata()

        try:
            await self._refresh_nodes_locked(return_state=ConnectionState.FAILING_OVER)
        except AppError as error:
            self._emit_log("warning", f"切换前刷新失败，使用缓存节点: {error}")

        attempted = {old_node.id}
        candidates = self.node_pool.eligible_nodes(
            preferred_country=old_node.country_code or old_node.country,
            exclude_ids=attempted,
        )
        for candidate in candidates:
            attempted.add(candidate.id)
            try:
                await self._connect_node_locked(candidate, failover=True)
                self._emit_log("info", f"已自动切换到 {candidate.country} {candidate.ip}")
                return
            except asyncio.CancelledError:
                raise
            except AppError as error:
                self._emit_log("warning", f"候选节点 {candidate.ip} 连接失败: {error}")

        error = AppError(
            ErrorCode.NO_STRICT_HOME_NODE,
            "没有可用于故障切换的严格家庭宽带节点",
        )
        await self._set_state(
            ConnectionState.ERROR,
            str(error),
            active_node_id=None,
            exit_ip="",
            connected_since=None,
            metadata=self._disconnected_metadata(),
        )
        raise error

    async def _probe_active_connection(self, node: VpnNode) -> tuple[bool, str]:
        try:
            if not await self.tunnel.is_connected():
                return False, "OpenVPN 隧道已断开"
            exit_ip = (await self.network.public_ip()).strip()
            verified_exit_ip = str(
                self._snapshot.metadata.get("verified_exit_ip") or node.ip
            ).strip()
            if not self._same_ip(exit_ip, verified_exit_ip):
                return (
                    False,
                    f"出口 IP 已由 {verified_exit_ip or '未知'} 变为 {exit_ip or '未知'}",
                )
            return True, ""
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return False, str(error)

    async def _cancel_connection_locked(self) -> None:
        try:
            if self._snapshot.state is not ConnectionState.DISCONNECTING:
                await self._set_state(ConnectionState.DISCONNECTING, "操作已取消，正在恢复网络")
            await self._cleanup_connection(suppress_errors=True)
            await self._set_state(
                ConnectionState.IDLE,
                "操作已取消",
                active_node_id=None,
                exit_ip="",
                connected_since=None,
                consecutive_failures=0,
                metadata=self._disconnected_metadata(),
            )
        except Exception as error:
            self._emit_log("error", f"取消操作后的清理失败: {error}")

    async def _cleanup_connection(self, *, suppress_errors: bool) -> list[Exception]:
        results = await asyncio.gather(
            self.tunnel.disconnect(),
            self.network.restore(),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        for error in errors:
            self._emit_log("warning", f"网络清理警告: {error}")
        return [] if suppress_errors else errors

    async def _build_config(self, node: VpnNode) -> Path:
        result = self.config_builder(node)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Path):
            result = Path(result)
        return result

    def _select_node(self, *, node_id: str | None, country: str) -> VpnNode | None:
        if node_id is not None:
            node = self.node_pool.get(node_id)
            return node if node is not None and self.node_pool.is_eligible(node) else None
        return self.node_pool.select(preferred_country=country)

    def _connection_candidates(
        self,
        first: VpnNode,
        *,
        preferred_country: str,
    ) -> list[VpnNode]:
        country = first.country_code or preferred_country
        remaining = self.node_pool.eligible_nodes(
            preferred_country=country,
            exclude_ids={first.id},
        )
        attempts = max(1, int(self.settings.connection_attempts))
        return [first, *remaining][:attempts]

    def _active_node(self) -> VpnNode | None:
        node_id = self._snapshot.active_node_id
        return self.node_pool.get(node_id) if node_id else None

    def _has_connection_context(self) -> bool:
        return bool(
            self._snapshot.active_node_id
            or self._snapshot.exit_ip
            or self._snapshot.connected_since
            or self._snapshot.metadata.get("tunnel_connected") is True
        )

    def _disconnected_metadata(self) -> dict[str, Any]:
        return {"clash_running": True} if self._snapshot.clash_exit_ip else {}

    async def _verify_exit(self, node: VpnNode, exit_ip: str) -> tuple[VpnNode, str]:
        try:
            normalized_exit_ip = str(ip_address(exit_ip.strip()))
        except ValueError as error:
            raise AppError(
                ErrorCode.EXIT_IP_MISMATCH,
                f"无法识别公网出口 IP：{exit_ip or '未知'}",
            ) from error

        if self._same_ip(normalized_exit_ip, node.ip):
            return node, "exact"

        if self._same_ip(normalized_exit_ip, self._snapshot.clash_exit_ip):
            raise AppError(
                ErrorCode.EXIT_IP_MISMATCH,
                f"公网出口仍为 Clash 中转 IP {normalized_exit_ip}，OpenVPN 未接管流量",
            )

        exit_node = VpnNode(
            id=f"verified-exit:{normalized_exit_ip}",
            ip=normalized_exit_ip,
            remote_host=normalized_exit_ip,
            remote_port=0,
            protocol="exit-verification",
            country_code="",
            country="",
        )
        try:
            classified = await self.classifier.classify([exit_node])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise AppError(
                ErrorCode.EXIT_IP_MISMATCH,
                f"无法复核公网出口 {normalized_exit_ip} 的住宅属性",
                detail=str(error),
            ) from error
        if len(classified) != 1:
            raise AppError(
                ErrorCode.EXIT_IP_MISMATCH,
                f"公网出口 {normalized_exit_ip} 的住宅属性复核结果无效",
            )
        verified = classified[0]
        if verified.purity_grade is not PurityGrade.STRICT_HOME:
            raise AppError(
                ErrorCode.EXIT_IP_MISMATCH,
                f"公网出口 {normalized_exit_ip} 未通过严格家庭宽带复核",
            )

        node_country = node.country_code.strip().upper()
        exit_country = verified.country_code.strip().upper()
        if not node_country or exit_country != node_country:
            raise AppError(
                ErrorCode.EXIT_IP_MISMATCH,
                f"公网出口国家 {exit_country or '未知'} 与节点国家 {node_country or '未知'} 不一致",
            )

        node_asn = self._asn_number(node.asn)
        exit_asn = self._asn_number(verified.asn)
        if not node_asn or exit_asn != node_asn:
            raise AppError(
                ErrorCode.EXIT_IP_MISMATCH,
                f"公网出口 ASN {verified.asn or '未知'} 与节点 ASN {node.asn or '未知'} 不一致",
            )
        return verified, "same_asn_nat"

    def _verified_exit_metadata(
        self,
        endpoint: VpnNode,
        verified_exit: VpnNode,
        *,
        verification: str,
    ) -> dict[str, Any]:
        metadata = self._node_metadata(
            verified_exit,
            clash_running=True,
            tunnel_connected=True,
            exit_verified=True,
        )
        metadata.update(
            {
                "latency_ms": endpoint.latency_ms,
                "vpn_endpoint_ip": endpoint.ip,
                "verified_exit_ip": verified_exit.ip,
                "exit_verification": verification,
            }
        )
        return metadata

    @staticmethod
    def _node_metadata(
        node: VpnNode,
        *,
        clash_running: bool | None = None,
        tunnel_connected: bool | None = None,
        exit_verified: bool | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "country": node.country,
            "country_code": node.country_code,
            "city": node.city,
            "isp": node.isp,
            "asn": node.asn,
            "latency_ms": node.latency_ms,
        }
        flags = {
            "clash_running": clash_running,
            "tunnel_connected": tunnel_connected,
            "exit_verified": exit_verified,
        }
        metadata.update({key: value for key, value in flags.items() if value is not None})
        return metadata

    async def _normalize_for_new_operation(self) -> None:
        state = self._snapshot.state
        if state in {ConnectionState.IDLE, ConnectionState.CONNECTED, ConnectionState.DEGRADED}:
            return
        if state is not ConnectionState.ERROR:
            await self._set_state(ConnectionState.ERROR, "已恢复上次未完成的操作")
        if self._has_connection_context():
            await self._cleanup_connection(suppress_errors=True)
        await self._set_state(
            ConnectionState.IDLE,
            "准备就绪",
            active_node_id=None,
            exit_ip="",
            connected_since=None,
            consecutive_failures=0,
            metadata=self._disconnected_metadata(),
        )

    def _refresh_return_state(self) -> ConnectionState:
        if self._snapshot.state in {
            ConnectionState.CONNECTED,
            ConnectionState.DEGRADED,
            ConnectionState.FAILING_OVER,
            ConnectionState.STARTING_CLASH,
        }:
            return self._snapshot.state
        if self._snapshot.state is ConnectionState.ERROR:
            return ConnectionState.ERROR
        return ConnectionState.IDLE

    @staticmethod
    def _require_ready_environment(reports: Sequence[EnvironmentReport]) -> None:
        failed = [
            f"{check.label}: {check.detail}"
            for report in reports
            for check in report.checks
            if check.required and not check.ok
        ]
        if failed:
            raise AppError(
                ErrorCode.ENVIRONMENT_NOT_READY,
                "运行环境未就绪",
                detail="; ".join(failed),
            )

    async def _prepare_environment(self) -> None:
        try:
            reports = await asyncio.gather(
                self.tunnel.check_environment(),
                self.network.check_environment(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise AppError(
                ErrorCode.ENVIRONMENT_NOT_READY,
                "运行环境检查失败",
                detail=str(error),
            ) from error
        if all(report.ready for report in reports):
            return

        try:
            repaired = await self.tunnel.repair_environment()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit_log("warning", f"运行环境自动修复失败：{error}")
            repaired = False
        if repaired:
            await self._set_state(
                ConnectionState.CHECKING_ENVIRONMENT,
                "已自动修复 OpenVPN 环境，正在复检",
            )
            self._emit_log("info", "已自动修复 OpenVPN 残留环境")
            reports = await asyncio.gather(
                self.tunnel.check_environment(),
                self.network.check_environment(),
            )
        self._require_ready_environment(reports)

    @staticmethod
    def _is_retryable_connection_error(error: AppError) -> bool:
        return error.code not in {
            ErrorCode.CLASH_NOT_FOUND,
            ErrorCode.CLASH_PORT_UNAVAILABLE,
            ErrorCode.OPENVPN_NOT_FOUND,
            ErrorCode.NETWORK_RESTORE_FAILED,
            ErrorCode.NO_STRICT_HOME_NODE,
            ErrorCode.VPNGATE_UNAVAILABLE,
        }

    async def _set_state(
        self,
        state: ConnectionState,
        message: str,
        **changes: Any,
    ) -> None:
        current = self._snapshot.state
        if state is not current and state not in _ALLOWED_TRANSITIONS[current]:
            raise RuntimeError(f"invalid connection state transition: {current} -> {state}")
        self._snapshot.state = state
        self._snapshot.message = message
        for key, value in changes.items():
            if not hasattr(self._snapshot, key):
                raise AttributeError(f"ConnectionSnapshot has no field {key!r}")
            setattr(self._snapshot, key, value)
        self._emit_snapshot()
        await self._persist_snapshot()

    async def _persist_nodes(self, nodes: Sequence[VpnNode]) -> None:
        if self.store is None:
            return
        try:
            await self.store.upsert_nodes(nodes)
            for node_id, until in self.node_pool.cooldowns().items():
                await self.store.save_cooldown(node_id, until)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit_log("warning", f"节点状态保存失败: {error}")

    async def _persist_node(
        self,
        node: VpnNode,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        if self.store is None:
            return
        try:
            await self.store.upsert_nodes([node])
            await self.store.save_cooldown(node.id, cooldown_until)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit_log("warning", f"节点状态保存失败: {error}")

    async def _persist_snapshot(self) -> None:
        if self.store is None:
            return
        try:
            await self.store.save_snapshot(self._copy_snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit_log("warning", f"连接状态保存失败: {error}")

    def _emit_snapshot(self) -> None:
        for listener in tuple(self._snapshot_listeners):
            self._invoke_callback(listener, self._copy_snapshot())

    def _emit_nodes(self, nodes: Sequence[VpnNode]) -> None:
        stable_nodes = tuple(nodes)
        for listener in tuple(self._node_listeners):
            self._invoke_callback(listener, stable_nodes)

    def _emit_log(self, level: str, message: str) -> None:
        for listener in tuple(self._log_listeners):
            self._invoke_callback(listener, level, message)

    @staticmethod
    def _invoke_callback(callback: Callable[..., Any], *args: Any) -> None:
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                task.add_done_callback(ConnectionOrchestrator._consume_callback_result)
        except Exception:
            # A UI/plugin callback must never stop network state recovery.
            return

    @staticmethod
    def _consume_callback_result(task: asyncio.Future[Any]) -> None:
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            return

    def _copy_snapshot(self) -> ConnectionSnapshot:
        return replace(self._snapshot, metadata=dict(self._snapshot.metadata))

    def _track_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        async with self._operation_lock:
            current = asyncio.current_task()
            previous = self._current_operation
            self._current_operation = current
            try:
                yield
            finally:
                if self._current_operation is current:
                    self._current_operation = previous

    @staticmethod
    def _remove_listener(listeners: list[Any], listener: Any) -> None:
        with suppress(ValueError):
            listeners.remove(listener)

    @staticmethod
    def _same_ip(left: str, right: str) -> bool:
        try:
            return ip_address(left.strip()) == ip_address(right.strip())
        except ValueError:
            return left.strip().casefold() == right.strip().casefold()

    @staticmethod
    def _asn_number(value: str) -> str:
        match = _ASN_PATTERN.search(value)
        return match.group(1) if match else ""

    @staticmethod
    def _as_connection_error(error: Exception) -> AppError:
        if isinstance(error, AppError):
            return error
        return AppError(
            ErrorCode.OPENVPN_CONNECT_FAILED,
            "OpenVPN 连接失败",
            detail=str(error),
        )


# A concise alias for integration code that prefers the application-level name.
AppOrchestrator = ConnectionOrchestrator

__all__ = ["AppOrchestrator", "ConfigBuilder", "ConnectionOrchestrator", "StateStore"]
