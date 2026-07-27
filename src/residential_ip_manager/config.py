from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def default_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "ResidentialIPManager"


@dataclass(slots=True)
class AppSettings:
    clash_host: str = "127.0.0.1"
    clash_port: int = 7890
    clash_controller_port: int = 9090
    refresh_interval_seconds: int = 300
    candidate_probe_interval_seconds: int = 30
    active_health_interval_seconds: int = 10
    failure_threshold: int = 3
    connection_attempts: int = 3
    cooldown_seconds: int = 600
    country_filter: str = ""
    strict_home_only: bool = True
    auto_failover: bool = True
    data_dir: Path = field(default_factory=default_data_dir)

    @classmethod
    def load(cls, path: Path) -> AppSettings:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if "data_dir" in data:
            data["data_dir"] = Path(data["data_dir"])
        return cls(**data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload["data_dir"] = str(self.data_dir)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
