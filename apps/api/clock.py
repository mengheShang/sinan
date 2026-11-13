"""The API's clock, a dependency so tests can fix the time."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def get_clock() -> Clock:
    """The current time; tests replace it to check expiry and grace periods."""
    return utc_now


ClockDep = Annotated[Clock, Depends(get_clock)]
