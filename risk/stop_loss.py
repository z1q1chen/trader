class ATRStopLoss:
    """
    ATR-based stop-loss: stop placed at entry - (multiplier * ATR).
    Adapts to market volatility — wider in volatile markets, tighter in calm ones.
    Also trails upward as price rises (trailing behavior built in).
    """

    def __init__(self, multiplier: float = 2.0):
        self.multiplier = multiplier
        self.stop_price: float | None = None
        self.peak_price: float | None = None
        self._current_atr: float = 0.0

    def on_entry(self, entry_price: float, current_atr: float) -> None:
        self._current_atr = current_atr
        self.peak_price = entry_price
        self.stop_price = entry_price - (self.multiplier * current_atr)

    def update(self, current_price: float) -> None:
        """Call on every bar. Trails the stop up if price makes new highs."""
        if self.peak_price is None:
            return
        if current_price > self.peak_price:
            self.peak_price = current_price
            self.stop_price = current_price - (self.multiplier * self._current_atr)

    def is_stopped(self, current_price: float) -> bool:
        if self.stop_price is None:
            return False
        return current_price <= self.stop_price

    def reset(self) -> None:
        self.stop_price = None
        self.peak_price = None
        self._current_atr = 0.0

    def to_dict(self) -> dict:
        """Serialize the ATRStopLoss state to a dictionary."""
        return {
            "multiplier": self.multiplier,
            "stop_price": self.stop_price,
            "peak_price": self.peak_price,
            "_current_atr": self._current_atr,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ATRStopLoss":
        """Deserialize an ATRStopLoss instance from a dictionary."""
        obj = cls(multiplier=data["multiplier"])
        obj.stop_price = data.get("stop_price")
        obj.peak_price = data.get("peak_price")
        obj._current_atr = data.get("_current_atr", 0.0)
        return obj
