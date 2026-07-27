from __future__ import annotations

import sys
from pathlib import Path

from residential_ip_manager.main import app_icon_path


def test_source_icon_path_points_to_multisize_ico() -> None:
    path = app_icon_path()

    assert path.name == "app-icon.ico"
    assert path.is_file()


def test_frozen_icon_path_uses_pyinstaller_bundle_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert app_icon_path() == (
        tmp_path / "residential_ip_manager" / "assets" / "app-icon.ico"
    )
