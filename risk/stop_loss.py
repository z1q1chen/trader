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
