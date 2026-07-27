from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from residential_ip_manager.domain.models import (  # noqa: E402
    ConnectionSnapshot,
    ConnectionState,
    NodeStatus,
    PurityGrade,
    VpnNode,
)
from residential_ip_manager.ui.main_window import MainWindow  # noqa: E402


def sample_node(index: int, country: str, isp: str, latency: int) -> VpnNode:
    octet = 100 + index
    return VpnNode(
        id=f"{country}_203.0.113.{octet}_443_tcp",
        ip=f"203.0.113.{octet}",
        remote_host=f"203.0.113.{octet}",
        remote_port=443,
        protocol="tcp",
        country_code=country,
        country={"JP": "日本", "KR": "韩国", "TH": "泰国"}[country],
        city={"JP": "东京", "KR": "首尔", "TH": "曼谷"}[country],
        isp=isp,
        asn=f"AS{4700 + index}",
        reverse_dns=f"node-{index}.home.ne.jp",
        score=180000 - index * 1000,
        speed_bps=80_000_000 - index * 2_000_000,
        sessions=5 + index,
        latency_ms=latency,
        purity_grade=PurityGrade.STRICT_HOME,
        purity_score=100,
        status=NodeStatus.AVAILABLE,
        last_checked_at=datetime.now(UTC),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成桌面界面预览图")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    application = QApplication([])
    window = MainWindow()
    nodes = [
        sample_node(1, "JP", "NTT Communications", 82),
        sample_node(2, "JP", "Sony Network Communications", 96),
        sample_node(3, "KR", "SK Broadband", 118),
        sample_node(4, "TH", "AIS Fibre", 154),
    ]
    window.set_nodes(nodes)
    window.set_snapshot(
        ConnectionSnapshot(
            state=ConnectionState.CONNECTED,
            message="连接正常，正在持续验证出口",
            active_node_id=nodes[0].id,
            exit_ip=nodes[0].ip,
            clash_exit_ip="198.51.100.24",
            connected_since=datetime.now(UTC),
            metadata={
                "country": nodes[0].country,
                "country_code": nodes[0].country_code,
                "isp": nodes[0].isp,
                "latency_ms": nodes[0].latency_ms,
                "purity_score": nodes[0].purity_score,
                "clash_running": True,
                "openvpn_connected": True,
            },
        )
    )
    window.append_log("info", "Clash 中转检测通过")
    window.append_log("info", "OpenVPN 初始化完成")
    window.resize(args.width, args.height)
    window.show()
    application.processEvents()

    output = args.output or (
        Path(__file__).resolve().parents[1] / "artifacts" / "ui-preview.png"
    )
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not window.grab().save(str(output)):
        raise RuntimeError("无法保存界面预览图")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
