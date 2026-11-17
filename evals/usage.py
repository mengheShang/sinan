"""Token and latency counters for live runs (Issue #43).

`Metered` wraps the SDK's Messages resource: it times each call and sums the
token counts the API reports. It keeps counts and timings only, never a
request or a reply. A call that raises is not counted; the time of the SDK's
own retries is part of the call that finally succeeds.
"""

import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.providers.anthropic_llm import Messages


@dataclass
class Metered:
    messages: Messages
    clock: Callable[[], float] = time.perf_counter
    seconds: list[float] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    def create(self, **kwargs: Any) -> Any:
        start = self.clock()
        response = self.messages.create(**kwargs)
        self.seconds.append(self.clock() - start)
        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        return response


def p90(values: Sequence[float]) -> float:
    """The nearest-rank 90th percentile."""
    ordered = sorted(values)
    rank = math.ceil(0.9 * len(ordered))
    return ordered[rank - 1]


def usage_data(meter: Metered) -> dict[str, Any]:
    """Calls, summed tokens, and median and p90 latency in milliseconds."""
    seconds = meter.seconds
    latency = None
    if seconds:
        median = statistics.median(seconds)
        latency = {"median": round(median * 1000), "p90": round(p90(seconds) * 1000)}
    return {
        "calls": len(seconds),
        "input_tokens": meter.input_tokens,
        "output_tokens": meter.output_tokens,
        "latency_ms": latency,
    }
