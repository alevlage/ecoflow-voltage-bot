"""Decides when the voltage is critically low or high.

A level must hold for `hold` seconds before it counts, so short spikes don't
trigger alerts, and the voltage must come back `hysteresis` volts inside the
limit before it counts as normal again, so it doesn't flap at the edge.
"""

from __future__ import annotations

NORMAL, LOW, HIGH = "normal", "low", "high"


class VoltageAlarm:
    def __init__(self, low: float, high: float, hold: float = 10.0, hysteresis: float = 3.0):
        self.low = low
        self.high = high
        self.hold = hold
        self.hysteresis = hysteresis
        self.level = NORMAL
        self._candidate = NORMAL
        self._candidate_since = 0.0

    def _raw_level(self, volts: float) -> str:
        if volts < self.low:
            return LOW
        if volts > self.high:
            return HIGH
        # Inside the limits, but stay in an alarm level until clearly back.
        if self.level == LOW and volts < self.low + self.hysteresis:
            return LOW
        if self.level == HIGH and volts > self.high - self.hysteresis:
            return HIGH
        return NORMAL

    def update(self, volts: float, now: float) -> str | None:
        """Feed a reading. Returns the new level when it changes, else None."""
        raw = self._raw_level(volts)
        if raw != self._candidate:
            self._candidate, self._candidate_since = raw, now
        if raw != self.level and now - self._candidate_since >= self.hold:
            self.level = raw
            return raw
        return None

    def message(self, level: str, volts: float) -> str:
        if level == LOW:
            return f"🔻 Voltage critically LOW: {volts:.1f} V (below {self.low:.0f} V)"
        if level == HIGH:
            return f"🔺 Voltage critically HIGH: {volts:.1f} V (above {self.high:.0f} V)"
        return f"✅ Voltage back to normal: {volts:.1f} V"
