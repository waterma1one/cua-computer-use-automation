"""Cost estimation for `run.json`: published per-million-token list prices, in USD, for the
models this project knows. A model not listed here gets no estimate and a note saying so --
a guessed price in an audit record is worse than an honest null. Prices change; these are
list prices as last checked and the estimate is labelled as one.
"""

from __future__ import annotations

from cua.llm.base import Usage

__all__ = ["estimate_cost_usd"]

# model -> (input USD per 1M tokens, output USD per 1M tokens)
_PER_MILLION_USD: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
}


def estimate_cost_usd(model: str, usage: Usage) -> tuple[float | None, str | None]:
    """`(estimated_cost_usd, cost_note)`: a rounded estimate and no note for a known model,
    `(None, reason)` otherwise. Output tokens are `total - prompt`, so reasoning tokens the
    provider bills as output are included."""
    prices = _PER_MILLION_USD.get(model)
    if prices is None:
        return None, f"no price on file for model {model!r}; token counts are exact"
    in_price, out_price = prices
    output = max(usage.total - usage.prompt, usage.completion)
    cost = (usage.prompt * in_price + output * out_price) / 1_000_000
    return round(cost, 6), "estimate from list prices per million tokens; may be out of date"
