from dataclasses import dataclass, field
from typing import Dict


@dataclass
class PairSettings:
    threshold: float = 0.50  # USD spread threshold for alert
    muted: bool = False
    last_alert_time: float = 0.0


@dataclass
class UserSettings:
    chat_id: int
    username: str = ""
    cooldown: int = 300  # seconds between alerts
    pair_settings: Dict[str, PairSettings] = field(default_factory=dict)

    def get_pair(self, pair: str) -> PairSettings:
        if pair not in self.pair_settings:
            self.pair_settings[pair] = PairSettings()
        return self.pair_settings[pair]
