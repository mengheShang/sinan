"""Which parts of a due time were assumed rather than written (ADR 0016).

Pure functions only. The flags are stored in JSON data as `zone_assumed`,
`time_assumed` and `date_computed`: in the `after` data of the item version that set the
due time (the create version, or an applied update) and in both sides of an
item_update proposal's payload. Data written before ADR 0016 has no flags,
which reads as nothing assumed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Assumed:
    # No zone was written, so the owner's zone was used
    zone: bool = False
    # No time was written, so 23:59 was used
    time: bool = False
    # The date is the email's date plus a count ("in 3 days")
    date: bool = False


NOTHING = Assumed()


def assumed_of(data: Mapping[str, Any] | None) -> Assumed:
    """The flags in stored data. Anything but a JSON true counts as false."""
    if data is None:
        return NOTHING
    zone = data.get("zone_assumed") is True
    time = data.get("time_assumed") is True
    date = data.get("date_computed") is True
    return Assumed(zone, time, date)


def assumed_fields(flags: Assumed) -> dict[str, bool]:
    """The flags as stored data."""
    return {
        "zone_assumed": flags.zone,
        "time_assumed": flags.time,
        "date_computed": flags.date,
    }
