"""
LLM-powered trading strategy.
Uses Claude to analyze market conditions and decide buy/sell/hold.
"""

import json
import logging
import os

import anthropic
import pandas as pd

from strategy.base import BaseStrategy
from indicators.moving_averages import add_sma
from indicators.volatility import compute_atr
from indicators.rsi import compute_rsi
from indicators.macd import compute_macd

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a quantitative crypto trading analyst. You analyze BTC/USDT market data and make trading decisions for a paper trading bot.

You will receive a market snapshot with technical indicators and recent price action. Your job is to decide: BUY, SELL, or HOLD.

Rules:
- You are managing a single-position paper trading account (can hold max 1 position at a time)
- If already in a position, you can only SELL or HOLD
- If flat (no position), you can only BUY or HOLD
- Be decisive — only HOLD if you genuinely see no clear setup
- Consider trend, momentum, volatility, and risk/reward
- Think about stop-loss placement (where would you exit if wrong?)

Respond with ONLY valid JSON (no markdown, no explanation outside the JSON):
{
  "action": "buy" | "sell" | "hold",
  "confidence": 0.0 to 1.0,
  "reasoning": "1-2 sentence explanation",
  "suggested_stop_pct": 0.01 to 0.10
}"""


def build_market_snapshot(
    df: pd.DataFrame,
    position: dict | None = None,
    lookback: int = 10,
) -> str:
    """Build a human-readable market snapshot for the LLM."""
    latest = df.iloc[-1]
    price = latest["close"]

    lines = [
        f"=== BTC/USDT Market Snapshot ===",
        f"Current Price: ${price:,.2f}",
        f"",
        f"--- Technical Indicators ---",
        f"SMA 10:      ${latest.get('sma_10', 0):,.2f}",
        f"SMA 50:      ${latest.get('sma_50', 0):,.2f}",
        f"RSI (14):    {latest.get('rsi', 0):.1f}",
        f"MACD:        {latest.get('macd', 0):.2f}",
        f"MACD Signal: {latest.get('macd_signal', 0):.2f}",
        f"MACD Hist:   {latest.get('macd_hist', 0):.2f}",
        f"ATR (14):    ${latest.get('atr', 0):,.2f}",
        f"",
        f"--- Trend Context ---",
    ]

    sma_10 = latest.get("sma_10", 0)
    sma_50 = latest.get("sma_50", 0)
    if sma_10 > sma_50:
        lines.append(f"SMA trend: BULLISH (short above long by ${sma_10 - sma_50:,.2f})")
    else:
        lines.append(f"SMA trend: BEARISH (short below long by ${sma_50 - sma_10:,.2f})")

    rsi = latest.get("rsi", 50)
    if rsi > 70:
        lines.append(f"RSI: OVERBOUGHT ({rsi:.1f})")
    elif rsi < 30:
        lines.append(f"RSI: OVERSOLD ({rsi:.1f})")
    else:
        lines.append(f"RSI: NEUTRAL ({rsi:.1f})")

    macd_hist = latest.get("macd_hist", 0)
    prev_hist = df.iloc[-2].get("macd_hist", 0) if len(df) > 1 else 0
    if macd_hist > 0 and macd_hist > prev_hist:
        lines.append("MACD: Bullish momentum INCREASING")
    elif macd_hist > 0:
        lines.append("MACD: Bullish but momentum FADING")
    elif macd_hist < 0 and macd_hist < prev_hist:
        lines.append("MACD: Bearish momentum INCREASING")
    else:
        lines.append("MACD: Bearish but momentum FADING")

    # Recent price action
    recent = df.tail(lookback)
    lines.append(f"")
    lines.append(f"--- Last {lookback} Candles ---")
    lines.append(f"{'Time':<22} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>12}")
    for ts, row in recent.iterrows():
        lines.append(
            f"{str(ts):<22} ${row['open']:>9,.2f} ${row['high']:>9,.2f} "
            f"${row['low']:>9,.2f} ${row['close']:>9,.2f} {row['volume']:>12,.1f}"
        )

    high = recent["high"].max()
    low = recent["low"].min()
    lines.append(f"")
    lines.append(f"Range: ${low:,.2f} - ${high:,.2f} (${high - low:,.2f})")

    # Position info
    lines.append(f"")
    lines.append(f"--- Current Position ---")
    if position:
        entry = position["entry_price"]
        unrealized = (price - entry) * position["qty"]
        pct = (price - entry) / entry * 100
        lines.append(f"Status: LONG {position['qty']:.6f} BTC")
        lines.append(f"Entry:  ${entry:,.2f}")
        lines.append(f"Unrealized P&L: ${unrealized:,.2f} ({pct:+.1f}%)")
        if position.get("stop_price"):
            lines.append(f"Stop-loss: ${position['stop_price']:,.2f}")
    else:
        lines.append("Status: FLAT (no position)")

    return "\n".join(lines)


class LLMStrategy(BaseStrategy):
    """Uses Claude to analyze indicators and decide trades."""

    def __init__(
        self,
        sma_short: int = 10,
        sma_long: int = 50,
        atr_period: int = 14,
        model: str = "claude-sonnet-4-20250514",
        min_confidence: float = 0.6,
    ):
        self.sma_short = sma_short
        self.sma_long = sma_long
        self.atr_period = atr_period
        self.model = model
        self.min_confidence = min_confidence

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not set. Export it: export ANTHROPIC_API_KEY=sk-ant-..."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = add_sma(df, self.sma_short, self.sma_long)
        df["atr"] = compute_atr(df, self.atr_period)
        df["rsi"] = compute_rsi(df)
        macd_df = compute_macd(df)
        df = pd.concat([df, macd_df], axis=1)
        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Placeholder — LLM strategy generates signals via analyze()."""
        df["buy_signal"] = False
        df["sell_signal"] = False
        return df

    def analyze(
        self,
        df: pd.DataFrame,
        position: dict | None = None,
    ) -> dict:
        """
        Call Claude with the market snapshot and get a trading decision.

        Returns:
            {"action": "buy"|"sell"|"hold", "confidence": float, "reasoning": str, "suggested_stop_pct": float}
        """
        snapshot = build_market_snapshot(df, position=position)

        logger.debug("LLM prompt:\n%s", snapshot)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=256,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": snapshot}],
            )

            text = response.content[0].text.strip()
            logger.info("[LLM] Raw response: %s", text)

            decision = json.loads(text)

            # Validate
            action = decision.get("action", "hold").lower()
            if action not in ("buy", "sell", "hold"):
                action = "hold"

            confidence = float(decision.get("confidence", 0))
            if confidence < self.min_confidence:
                logger.info(
                    "[LLM] Confidence %.2f below threshold %.2f — forcing HOLD",
                    confidence, self.min_confidence,
                )
                action = "hold"

            return {
                "action": action,
                "confidence": confidence,
                "reasoning": decision.get("reasoning", ""),
                "suggested_stop_pct": float(decision.get("suggested_stop_pct", 0.03)),
            }

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error("[LLM] Failed to parse response: %s", e)
            return {
                "action": "hold",
                "confidence": 0.0,
                "reasoning": f"Parse error: {e}",
                "suggested_stop_pct": 0.03,
            }
        except anthropic.APIError as e:
            logger.error("[LLM] API error: %s", e)
            return {
                "action": "hold",
                "confidence": 0.0,
                "reasoning": f"API error: {e}",
                "suggested_stop_pct": 0.03,
            }
