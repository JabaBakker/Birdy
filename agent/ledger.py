"""Cost ledger: tracks spend per day so the agent can enforce its own budget cap."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def spent_today(self) -> float:
        return float(self._load().get(date.today().isoformat(), {}).get("usd", 0.0))

    def cycles_total(self) -> int:
        return sum(int(day.get("cycles", 0)) for day in self._load().values())

    def record(self, usd: float) -> None:
        data = self._load()
        key = date.today().isoformat()
        day = data.setdefault(key, {"usd": 0.0, "cycles": 0})
        day["usd"] = float(day["usd"]) + float(usd or 0.0)
        day["cycles"] = int(day["cycles"]) + 1
        self.path.write_text(json.dumps(data, indent=2))

    def within_budget(self, daily_cap: float) -> bool:
        return self.spent_today() < daily_cap
