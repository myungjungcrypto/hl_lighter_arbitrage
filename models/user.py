from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class PairSettings:
    threshold: float = 0.50  # USD spread threshold for alert
    muted: bool = False
    last_alert_time: float = 0.0


@dataclass
class MarkIndexPairSettings:
    above_threshold: Optional[float] = None  # alert when gap > X% (None = disabled)
    below_threshold: Optional[float] = None  # alert when gap < X% (None = disabled)
    muted: bool = False
    cooldown: int = 300
    last_alert_time: float = 0.0


@dataclass
class UserSettings:
    chat_id: int
    username: str = ""
    cooldown: int = 300  # seconds between spread alerts
    pair_settings: Dict[str, PairSettings] = field(default_factory=dict)
    mark_index_settings: Dict[str, MarkIndexPairSettings] = field(default_factory=dict)

    def get_pair(self, pair: str) -> PairSettings:
        if pair not in self.pair_settings:
            self.pair_settings[pair] = PairSettings()
        return self.pair_settings[pair]

    def get_mark_index(self, pair: str) -> MarkIndexPairSettings:
        if pair not in self.mark_index_settings:
            self.mark_index_settings[pair] = MarkIndexPairSettings()
        return self.mark_index_settings[pair]
