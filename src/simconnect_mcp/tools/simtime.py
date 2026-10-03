"""Simulator clock tool — set the in-sim date/time via the ZULU_*_SET events."""

from __future__ import annotations

import asyncio
from typing import Annotated

from pydantic import Field

from simconnect_mcp.connection import SimConnectManager
from simconnect_mcp.tools import handle_simconnect_errors, require_connection
from simconnect_mcp.tools.events import trigger_event
from simconnect_mcp.tools.models import OkModel, ToolError

_DAY = 86400

# The sim applies a clock event almost at once, but the read-back is the only
# proof that it did, so give it a moment before reading.
_SETTLE_S = 0.3


class SimTimeResult(OkModel):
    sent_zulu: str = Field(..., description="Zulu HH:MM that was sent to the sim")
    utc_offset_min: int = Field(
        ..., description="UTC offset used for the conversion (0 when local=False)"
    )
    read_back_zulu: str | None = Field(
        None, description="Zulu HH:MM read back from the sim; null if the read failed"
    )
    read_back_local: str | None = Field(
        None, description="Local HH:MM read back from the sim; null if the read failed"
    )
    read_back_day_of_year: int | None = None
    verified: bool = Field(
        ..., description="True when the read-back matches the sent hour and minute"
    )
    summary_cs: str
    warning: str | None = None


def compute_target(
    hour: int,
    minute: int,
    local: bool,
    zulu_seconds: float,
    local_seconds: float,
) -> dict:
    """Turn a requested clock time into the Zulu hour/minute to send.

    The UTC offset is derived from the sim's own ZULU_TIME / LOCAL_TIME, so it
    follows the position and date the sim is currently at. ``day_shift`` is
    -1/0/+1 and tells the caller that the conversion crossed a UTC midnight.
    """
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0-23, got {hour}")
    if not 0 <= minute <= 59:
        raise ValueError(f"minute must be 0-59, got {minute}")

    offset_min = 0
    if local:
        diff = (local_seconds - zulu_seconds) % _DAY
        if diff > _DAY / 2:
            diff -= _DAY
        offset_min = round(diff / 60)

    total = hour * 60 + minute - offset_min
    day_shift = total // 1440
    total %= 1440
    return {
        "zulu_hour": total // 60,
        "zulu_minute": total % 60,
        "utc_offset_min": offset_min,
        "day_shift": day_shift,
    }


def _hm(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    s = int(seconds) % _DAY
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}"


@handle_simconnect_errors
@require_connection
async def set_sim_time(
    hour: Annotated[int, Field(description="Hour 0-23", ge=0, le=23)],
    minute: Annotated[int, Field(description="Minute 0-59", ge=0, le=59)] = 0,
    local: Annotated[
        bool,
        Field(description="True: hour/minute are local time at the current position. "
                          "False: they are Zulu/UTC."),
    ] = True,
    day_of_year: Annotated[
        int | None,
        Field(description="Optional Zulu day of year, 1-366", ge=1, le=366),
    ] = None,
    year: Annotated[
        int | None, Field(description="Optional Zulu year", ge=1, le=9999)
    ] = None,
) -> SimTimeResult | ToolError:
    """Set the simulator clock (changes sun position and weather timing).

    CZ: nastavit čas v simulátoru, hodina, minuta, místní čas, zulu, soumrak, den, noc.

    Sends ZULU_HOURS_SET / ZULU_MINUTES_SET (and optionally ZULU_DAYS_SET /
    ZULU_YEARS_SET), then reads the clock back and reports whether it matches.
    Not meant for use in flight.
    """
    manager = SimConnectManager()

    def _read_clock() -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for key, var in (
            ("zulu", "ZULU_TIME"),
            ("local", "LOCAL_TIME"),
            ("doy", "ZULU_DAY_OF_YEAR"),
        ):
            try:
                value = manager.accessor.read(var, unit="seconds" if key != "doy" else "number")
                out[key] = float(value) if value is not None else None
            except Exception:
                out[key] = None
        return out

    before = await manager.run_sync(_read_clock)
    if before["zulu"] is None or before["local"] is None:
        return ToolError(
            error="CLOCK_UNAVAILABLE",
            message="ZULU_TIME / LOCAL_TIME could not be read, so nothing was changed.",
            suggestion="Check the sim is running (msfs_get_connection_status) and loaded "
                       "into a flight, not the menu.",
        )

    target = compute_target(hour, minute, local, before["zulu"], before["local"])

    # Larger units first: setting the day or year can reset the clock.
    steps: list[tuple[str, int]] = []
    if year is not None:
        steps.append(("ZULU_YEARS_SET", year))
    if day_of_year is not None:
        steps.append(("ZULU_DAYS_SET", day_of_year))
    steps.append(("ZULU_HOURS_SET", target["zulu_hour"]))
    steps.append(("ZULU_MINUTES_SET", target["zulu_minute"]))

    for event_name, value in steps:
        sent = await trigger_event(event_name, value)
        if isinstance(sent, ToolError):
            return sent

    await asyncio.sleep(_SETTLE_S)
    after = await manager.run_sync(_read_clock)

    sent_zulu = f"{target['zulu_hour']:02d}:{target['zulu_minute']:02d}"
    read_zulu = _hm(after["zulu"])
    verified = read_zulu == sent_zulu
    warnings = []
    if not verified:
        warnings.append(
            f"Sent {sent_zulu}Z but the sim reads {read_zulu or 'nothing'}Z. The sim may "
            "have ignored the event, or real-time weather/time sync overrode it."
        )
    if target["day_shift"] and day_of_year is None:
        warnings.append(
            f"The Zulu conversion crossed UTC midnight ({target['day_shift']:+d} day); "
            "the date was not changed. Pass day_of_year to set it."
        )

    summary = (
        f"Čas v simu nastaven na {sent_zulu}Z; po zpětném čtení zulu {read_zulu}, "
        f"místní {_hm(after['local'])}."
        if verified
        else f"Odeslal jsem {sent_zulu}Z, ale sim hlásí zulu {read_zulu} — nepotvrzeno."
    )
    return SimTimeResult(
        sent_zulu=sent_zulu,
        utc_offset_min=target["utc_offset_min"],
        read_back_zulu=read_zulu,
        read_back_local=_hm(after["local"]),
        read_back_day_of_year=None if after["doy"] is None else int(after["doy"]),
        verified=verified,
        summary_cs=summary,
        warning=" ".join(warnings) if warnings else None,
    )
