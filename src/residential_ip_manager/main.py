from __future__ import annotations

import argparse
import contextlib
import ctypes
import logging
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from residential_ip_manager.application.orchestrator import ConnectionOrchestrator
from residential_ip_manager.config import AppSettings, default_data_dir
from residential_ip_manager.domain.errors import AppError, ErrorCode
from residential_ip_manager.domain.models import VpnNode
from residential_ip_manager.infrastructure.classification import StrictResidentialClassifier
from residential_ip_manager.infrastructure.ovpn_config import SafeOpenVpnConfigGenerator
from residential_ip_manager.infrastructure.probe import TcpNodeProbe
from residential_ip_manager.infrastructure.vpngate import VpnGateNodeSource
from residential_ip_manager.logging_setup import configure_logging
from residential_ip_manager.platform.clash import ClashVergeController
from residential_ip_manager.platform.network import WindowsNetworkController
from residential_ip_manager.platform.openvpn import OpenVpnController
from residential_ip_manager.platform.windows_environment import (
    AsyncSubprocessRunner,
    WindowsEnvironmentDetector,
    is_user_admin,
)
from residential_ip_manager.runtime import DesktopBridge
from residential_ip_manager.storage.sqlite_store import SQLiteStore
from residential_ip_manager.ui.main_window import MainWindow

LOGGER = logging.getLogger(__name__)
APP_USER_MODEL_ID = "ResidentialIPManager.Desktop.2"


def app_icon_path() -> Path:
    if getattr(sys, "frozen", False):
        bundle_root = Path(str(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
        return bundle_root / "residential_ip_manager" / "assets" / "app-icon.ico"
    return Path(__file__).resolve().parent / "assets" / "app-icon.ico"


def configure_windows_identity() -> None:
    if sys.platform != "win32":
        return
    with contextlib.suppress(OSError, AttributeError):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
            APP_USER_MODEL_ID
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="家宽出口控制台")
    parser.add_argument("--debug", action="store_true", help="同时向控制台输出调试日志")
    parser.add_argument("--no-elevate", action="store_true", help="不请求管理员权限")
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-seconds", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def request_elevation(arguments: list[str]) -> bool:
    if sys.platform != "win32" or is_user_admin():
        return False
    forwarded = [item for item in arguments if item not in {"--elevated", "--no-elevate"}]
    forwarded.append("--elevated")
    if getattr(sys, "frozen", False):
        executable = sys.executable
        parameters = subprocess.list2cmdline(forwarded)
    else:
        executable = sys.executable
        parameters = subprocess.list2cmdline(["-m", "residential_ip_manager.main", *forwarded])
    result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None,
        "runas",
        executable,
        parameters,
        str(Path.cwd()),
        1,
    )
    return result > 32


def create_runtime(
    settings: AppSettings,
) -> tuple[ConnectionOrchestrator, WindowsEnvironmentDetector]:
    runner = AsyncSubprocessRunner()
    detector = WindowsEnvironmentDetector(runner=runner)
    clash_config = detector.clash_config()
    if clash_config is not None:
        settings.clash_host = "127.0.0.1"
        settings.clash_port = clash_config.mixed_port
        settings.clash_controller_port = clash_config.external_controller_port

    data_dir = settings.data_dir
    config_generator = SafeOpenVpnConfigGenerator(
        proxy_host=settings.clash_host,
        proxy_port=settings.clash_port,
    )

    openvpn_logger = logging.getLogger("openvpn")
    clash = ClashVergeController(
        host=settings.clash_host,
        port=settings.clash_port,
        runner=runner,
        detector=detector,
    )

    async def build_config(node: VpnNode) -> Path:
        bypass_ips = await clash.discover_bypass_ips()
        if not bypass_ips:
            raise AppError(
                ErrorCode.ENVIRONMENT_NOT_READY,
                "无法识别 Clash 上游服务器，已停止连接以避免路由回环",
            )
        return config_generator.generate(
            node,
            data_dir / "runtime",
            bypass_ips=bypass_ips,
        )

    clash_http_proxy = f"http://{settings.clash_host}:{settings.clash_port}"
    orchestrator = ConnectionOrchestrator(
        source=VpnGateNodeSource(proxy_url=clash_http_proxy),
        classifier=StrictResidentialClassifier(proxy_url=clash_http_proxy),
        probe=TcpNodeProbe(max_concurrency=32),
        clash=clash,
        tunnel=OpenVpnController(
            runner=runner,
            detector=detector,
            log_listener=lambda source, message: openvpn_logger.info("%s: %s", source, message),
        ),
        network=WindowsNetworkController(
            runner=runner,
            snapshot_path=data_dir / "network_snapshot.json",
        ),
        config_builder=build_config,
        settings=settings,
        store=SQLiteStore(data_dir / "state.db"),
    )
    return orchestrator, detector


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(arguments)
    if not args.no_elevate and not args.elevated and request_elevation(arguments):
        return 0

    data_dir = default_data_dir()
    settings_path = data_dir / "settings.json"
    try:
        settings = AppSettings.load(settings_path)
    except (OSError, ValueError, TypeError):
        settings = AppSettings(data_dir=data_dir)
    settings.data_dir = data_dir
    configure_logging(data_dir, debug=args.debug)

    configure_windows_identity()
    application = QApplication([sys.argv[0], *arguments])
    application.setApplicationName("家宽出口控制台")
    application.setOrganizationName("ResidentialIPManager")
    application.setQuitOnLastWindowClosed(True)
    icon = QIcon(str(app_icon_path()))
    if not icon.isNull():
        application.setWindowIcon(icon)

    window = MainWindow()
    if not icon.isNull():
        window.setWindowIcon(icon)
    orchestrator, detector = create_runtime(settings)
    bridge = DesktopBridge(
        window=window,
        orchestrator=orchestrator,
        settings=settings,
        settings_path=settings_path,
        detector=detector,
    )
    application.aboutToQuit.connect(bridge.shutdown)
    window.show()
    bridge.start()
    if args.smoke_seconds > 0:
        QTimer.singleShot(args.smoke_seconds * 1000, application.quit)
    LOGGER.info("application started")
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
