from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path

from residential_ip_manager.domain.errors import AppError, ErrorCode
from residential_ip_manager.domain.models import ComponentCheck, EnvironmentReport
from residential_ip_manager.platform.windows_environment import (
    AsyncSubprocessRunner,
    CommandRunner,
    ProxySettings,
    ProxyStore,
    WindowsRegistryProxyStore,
    is_windows,
    local_app_data,
)


def default_proxy_snapshot_path() -> Path:
    return local_app_data() / "ResidentialIPManager" / "proxy-snapshot.json"


class WindowsNetworkController:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        proxy_store: ProxyStore | None = None,
        snapshot_path: Path | None = None,
        curl_executable: str = "curl.exe",
        public_ip_url: str = "https://api.ipify.org",
    ) -> None:
        self.runner = runner or AsyncSubprocessRunner()
        self.proxy_store = proxy_store or WindowsRegistryProxyStore()
        self.snapshot_path = snapshot_path or default_proxy_snapshot_path()
        self.curl_executable = curl_executable
        self.public_ip_url = public_ip_url
        self._snapshot: ProxySettings | None = None
        self._prepared = False
        self._operation_lock = asyncio.Lock()

    async def check_environment(self) -> EnvironmentReport:
        if not is_windows():
            return EnvironmentReport(
                [ComponentCheck("windows", "Windows", False, "Windows is required")]
            )
        try:
            settings = self.proxy_store.read()
        except OSError as exc:
            return EnvironmentReport(
                [ComponentCheck("system_proxy", "System proxy", False, str(exc))]
            )
        return EnvironmentReport(
            checks=[
                ComponentCheck("windows", "Windows", True, "Windows platform available"),
                ComponentCheck(
                    "system_proxy",
                    "System proxy",
                    True,
                    f"enabled={settings.enabled}, server={settings.server or '-'}",
                ),
            ]
        )

    async def prepare(self) -> None:
        """Snapshot once, then disable only the Windows system proxy switch."""
        async with self._operation_lock:
            if self._prepared:
                return
            if not is_windows():
                raise AppError(ErrorCode.ENVIRONMENT_NOT_READY, "Windows networking is required")

            current = self.proxy_store.read()
            snapshot = self._load_persisted_snapshot() or current
            self._persist_snapshot(snapshot)
            disabled = replace(current, enabled=False)
            try:
                self.proxy_store.write(disabled)
            except OSError as exc:
                try:
                    self.proxy_store.write(snapshot)
                except OSError as restore_exc:
                    raise AppError(
                        ErrorCode.NETWORK_RESTORE_FAILED,
                        "Failed to disable the system proxy and roll back its snapshot",
                        detail=f"write={exc}; rollback={restore_exc}",
                    ) from restore_exc
                self._remove_persisted_snapshot()
                raise AppError(
                    ErrorCode.ENVIRONMENT_NOT_READY,
                    "Failed to disable the Windows system proxy",
                    detail=str(exc),
                ) from exc

            self._snapshot = snapshot
            self._prepared = True

    async def restore(self) -> None:
        async with self._operation_lock:
            snapshot = self._snapshot or self._load_persisted_snapshot()
            if snapshot is None:
                return
            try:
                self.proxy_store.write(snapshot)
            except OSError as exc:
                raise AppError(
                    ErrorCode.NETWORK_RESTORE_FAILED,
                    "Failed to restore the Windows system proxy snapshot",
                    detail=str(exc),
                ) from exc
            self._snapshot = None
            self._prepared = False
            self._remove_persisted_snapshot()

    async def public_ip(self) -> str:
        try:
            result = await self.runner.run(
                [
                    self.curl_executable,
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--max-time",
                    "15",
                    self.public_ip_url,
                ],
                timeout=20,
            )
        except (OSError, TimeoutError) as exc:
            raise AppError(
                ErrorCode.ENVIRONMENT_NOT_READY,
                "Unable to query the public IP",
                detail=str(exc),
            ) from exc
        candidate = result.stdout.strip()
        if result.returncode != 0:
            raise AppError(
                ErrorCode.ENVIRONMENT_NOT_READY,
                "Unable to query the public IP",
                detail=result.stderr.strip(),
            )
        try:
            ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise AppError(
                ErrorCode.ENVIRONMENT_NOT_READY,
                "The public IP service returned invalid data",
                detail=candidate,
            ) from exc
        return candidate

    def _persist_snapshot(self, snapshot: ProxySettings) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
        payload = json.dumps(asdict(snapshot), ensure_ascii=True, indent=2)
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.snapshot_path)

    def _load_persisted_snapshot(self) -> ProxySettings | None:
        if not self.snapshot_path.is_file():
            return None
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            return ProxySettings(**payload)
        except (OSError, ValueError, TypeError):
            return None

    def _remove_persisted_snapshot(self) -> None:
        with suppress(FileNotFoundError):
            self.snapshot_path.unlink()


SystemNetworkController = WindowsNetworkController
