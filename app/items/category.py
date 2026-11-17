"""Notice categories and the morning grouping (ADR 0020). Pure functions only.

The model picks one category per notice from a fixed list. Anything else, or
none, reads as `other`. A category only decides which group a notice is
shown in: it never changes an item's kind, status or due time.

The category is stored in the `after` data of the item version that created
the notice, like the ADR 0016 flags; no column holds it. Data written before
ADR 0020 has none, which reads as `other`.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

# In the order the groups are shown
CATEGORIES = ("course", "safety", "event", "health", "career", "orders", "other")
OTHER = "other"


def category_of(value: object) -> str:
    """A known category, or `other` for anything else."""
    if isinstance(value, str) and value in CATEGORIES:
        return value
    return OTHER


def stored_category(data: Mapping[str, Any] | None) -> str:
    """The category in a version's `after` data; `other` when it has none."""
    if data is None:
        return OTHER
    return category_of(data.get("category"))


def group[T](items: Iterable[T], key: Callable[[T], str]) -> list[tuple[str, list[T]]]:
    """One group per category that has items, in CATEGORIES order.

    Items keep their order inside a group. `key` is read through
    `category_of`, so an unknown value lands in `other`.
    """
    groups: dict[str, list[T]] = {name: [] for name in CATEGORIES}
    for item in items:
        groups[category_of(key(item))].append(item)
    return [(name, found) for name, found in groups.items() if found]
