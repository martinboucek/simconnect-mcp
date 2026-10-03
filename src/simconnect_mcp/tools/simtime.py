"""Simulator clock tool — set the in-sim date/time via the ZULU_*_SET events."""

from __future__ import annotations

from typing import Any

from simconnect_mcp.connection import SimConnectManager
from simconnect_mcp.tools import handle_simconnect_errors, require_connection

_DAY = 86400


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


@handle_simconnect_errors
@require_connection
async def set_sim_time(
    hour: int,
    minute: int = 0,
    local: bool = True,
    day_of_year: int | None = None,
    year: int | None = None,
) -> dict:
    """Set the simulator clock (changes sim time, sun position and weather timing).

    CZ: nastavit čas v simulátoru, hodina, minuta, místní čas, zulu, soumrak, den, noc.

    Sends ZULU_HOURS_SET / ZULU_MINUTES_SET (and optionally ZULU_DAYS_SET /
    ZULU_YEARS_SET), then reads the clock back. Do not use in flight.

    Args:
        hour: Hour 0-23.
        minute: Minute 0-59.
        local: True = hour/minute are local time at the current position
            (default); False = Zulu/UTC.
        day_of_year: Optional Zulu day of year 1-366.
        year: Optional Zulu year.

    Returns:
        Requested and read-back time, with ``summary_cs``.
    """
    manager = SimConnectManager()

    def _read_clock() -> dict[str, Any]:
        return {
            "zulu_seconds": manager.aq.get("ZULU_TIME"),
            "local_seconds": manager.aq.get("LOCAL_TIME"),
        }

    before = await manager.run_sync(_read_clock)
    if before["zulu_seconds"] is None or before["local_seconds"] is None:
        return {
            "status": "error",
            "error": "CLOCK_UNAVAILABLE",
            "message": "ZULU_TIME / LOCAL_TIME could not be read.",
            "summary_cs": "Nelze přečíst čas simu, nic jsem nenastavoval.",
        }

    target = compute_target(
        hour, minute, local, before["zulu_seconds"], before["local_seconds"]
    )

    def _fire() -> None:
        steps = []
        if year is not None:
            steps.append(("ZULU_YEARS_SET", year))
        if day_of_year is not None:
            steps.append(("ZULU_DAYS_SET", day_of_year))
        steps.append(("ZULU_HOURS_SET", target["zulu_hour"]))
        steps.append(("ZULU_MINUTES_SET", target["zulu_minute"]))
        for event_name, value in steps:
            event = manager.ae.find(event_name)
            if event is None:
                raise ValueError(f"Event '{event_name}' not found")
            event(value)

    await manager.run_sync(_fire)
    after = await manager.run_sync(_read_clock)

    def _hms(seconds: float | None) -> str | None:
        if seconds is None:
            return None
        s = int(seconds) % _DAY
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}"

    result: dict[str, Any] = {
        "status": "ok",
        "requested": {
            "hour": hour,
            "minute": minute,
            "local": local,
            "day_of_year": day_of_year,
            "year": year,
        },
        "sent_zulu": f"{target['zulu_hour']:02d}:{target['zulu_minute']:02d}",
        "utc_offset_min": target["utc_offset_min"],
        "read_back_zulu": _hms(after["zulu_seconds"]),
        "read_back_local": _hms(after["local_seconds"]),
        "summary_cs": (
            f"Čas v simu nastaven na {target['zulu_hour']:02d}:"
            f"{target['zulu_minute']:02d}Z; po zpětném čtení zulu "
            f"{_hms(after['zulu_seconds'])}, místní {_hms(after['local_seconds'])}."
        ),
    }
    if target["day_shift"] and day_of_year is None:
        result["warning"] = (
            f"Přepočet na zulu přeskočil půlnoc ({target['day_shift']:+d} den); "
            "datum jsem neměnil, případně ho nastav přes day_of_year."
        )
    return result
