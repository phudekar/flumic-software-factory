"""Optional operator-maintained price estimates; never substitutes missing usage with zero."""

import json
import math
from pathlib import Path


class Pricing:
    def __init__(self, path=None):
        self.models = json.loads(Path(path).read_text())["models"] if path else {}
        for prices in self.models.values():
            for key, value in prices.items():
                if key in {"input", "output", "cached_input", "cache_creation"} and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError(
                        "Model prices must be nonnegative finite USD per million tokens"
                    )

    def estimate(self, provider, model, usage):
        price = self.models.get(f"{provider}/{model}")
        if price is None or usage.input_tokens is None or usage.output_tokens is None:
            return None
        if "input" not in price or "output" not in price:
            return None
        if "cached_input" in price and usage.cached_input_tokens is None:
            return None
        cached = usage.cached_input_tokens or 0
        creation = usage.cache_creation_tokens or 0
        if creation and "cache_creation" not in price:
            return None
        return (
            (usage.input_tokens - cached) * price["input"]
            + cached * price.get("cached_input", price["input"])
            + usage.output_tokens * price["output"]
            + creation * price.get("cache_creation", 0)
        ) / 1_000_000
