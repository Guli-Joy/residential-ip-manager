from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from residential_ip_manager.domain.models import ComponentCheck, EnvironmentReport  # noqa: E402
from residential_ip_manager.ui.environment_dialog import EnvironmentDialog  # noqa: E402


def _application() -> QApplication:
    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else QApplication([])


def test_repair_button_requires_confirmation_and_emits_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _application()
    dialog = EnvironmentDialog()
    action = "repair_orphaned_openvpn_routes"
    dialog.set_report(
        EnvironmentReport(
            [
                ComponentCheck(
                    "split_default_route_conflict",
                    "OpenVPN 残留路由",
                    False,
                    "可修复",
                    repair_action=action,
                    repair_targets=(
                        "0.0.0.0/1 -> 10.211.1.42 | OpenVPN TAP-Windows6 (接口 5)",
                    ),
                )
            ]
        )
    )
    emitted: list[str] = []
    dialog.repair_requested.connect(emitted.append)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    assert not dialog.repair_button.isHidden()
    dialog.repair_button.click()

    assert emitted == [action]
    assert not dialog.repair_button.isEnabled()
    assert not dialog.check_button.isEnabled()
    assert not dialog.progress.isHidden()
    assert dialog.summary_title.text() == "正在修复"
    dialog.close()


def test_repair_button_stays_hidden_for_non_repairable_failure() -> None:
    _application()
    dialog = EnvironmentDialog()
    dialog.set_report(
        EnvironmentReport([ComponentCheck("openvpn", "OpenVPN", False, "未安装")])
    )

    assert dialog.repair_button.isHidden()
    dialog.close()
