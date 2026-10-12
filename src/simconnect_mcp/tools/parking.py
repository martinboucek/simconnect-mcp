"""Parking spot (gate/stand) lookup on top of parking.py.

Local addition, not in upstream. Uses its own short SimConnect connection
through the MSFS SDK DLL -- see parking.py's module docstring -- so it does
not go through @require_connection or SimConnectManager.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from pydantic import BaseModel, Field

from simconnect_mcp.parking import (
    DLL_CANDIDATES,
    DLL_ENV_VAR,
    GATE_TYPES,
    ParkingError,
    fetch_parking,
    find_sdk_dll,
    select_spots,
)
from simconnect_mcp.tools import handle_simconnect_errors
from simconnect_mcp.tools.models import OkModel, ToolError


class ParkingSpot(BaseModel):
    name: str = Field(..., description="Name as the sim lists it, e.g. 'GATE A 12'")
    type: str = Field(..., description="SimConnect parking type, e.g. GATE_MEDIUM")
    radius_m: float = Field(..., description="Spot radius in metres as authored by the scenery")
    heading_deg: float = Field(..., description="Spot heading, degrees true")
    has_jetway: bool
    index: int = Field(..., description="Position in the sim's parking list")
    bias_x_m: float = Field(..., description="Offset from the airport reference point, metres")
    bias_z_m: float = Field(..., description="Offset from the airport reference point, metres")


class ParkingSpotsResult(OkModel):
    icao: str
    total_spots: int = Field(..., description="Every spot the sim returned, before filtering")
    gates: int
    gates_with_jetway: int
    returned: int
    spots: list[ParkingSpot]
    summary_cs: str


@handle_simconnect_errors
async def get_parking_spots(
    icao: Annotated[
        str, Field(description="Airport ICAO, e.g. 'WBSB'", min_length=3, max_length=6)
    ],
    gates_only: Annotated[bool, Field(description="Only GATE_* spots")] = False,
    min_radius_m: Annotated[
        float | None,
        Field(description="Hide spots smaller than this. Default scenery often authors gates "
                          "far smaller than the aircraft using them, so leave unset unless needed.",
              ge=0),
    ] = None,
    include_service: Annotated[bool, Field(description="Also list FUEL and VEHICLE spots")] = False,
    limit: Annotated[int, Field(description="Max spots returned", ge=1, le=500)] = 60,
) -> ParkingSpotsResult | ToolError:
    """List an airport's parking spots (gates, stands, ramps) as the loaded scenery defines them.

    CZ: stání, gate, parkovací místo, kam zaparkovat, nástupní most, jetway, odlet, přílet, GSX.

    Names match the sim's world-map parking list and what GSX sees (unless a GSX
    profile overrides the airport). Sorted jetway gates first, then other gates,
    then ramps, larger before smaller. Works for any airport, not just nearby
    ones, but reflects the scenery installed on this machine. Read-only; needs
    MSFS running and the MSFS SDK SimConnect.dll.
    """
    dll_path = find_sdk_dll()
    if dll_path is None:
        return ToolError(
            error="SDK_DLL_NOT_FOUND",
            message="The parking lookup needs SimConnect.dll from the MSFS SDK; the one "
                    "bundled with the Python package lacks the Facility Data API.",
            suggestion=f"Install the MSFS SDK or set {DLL_ENV_VAR} to its SimConnect.dll. "
                       f"Looked in: {', '.join(DLL_CANDIDATES)}",
        )

    icao = icao.strip().upper()
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, fetch_parking, icao, dll_path)
    except ParkingError as exc:
        return ToolError(
            error=exc.code,
            message=str(exc),
            suggestion="Check MSFS is running (msfs_get_connection_status).",
        )

    if data.exception is not None and not data.spots:
        return ToolError(
            error="AIRPORT_NOT_FOUND",
            message=f"The sim rejected the request for {icao} "
                    f"(SimConnect exception {data.exception}).",
            suggestion="Check the ICAO; msfs_get_facility_info confirms whether the sim knows it.",
        )
    if not data.complete and not data.spots:
        return ToolError(
            error="TIMEOUT",
            message=f"No parking data for {icao} arrived in time.",
            suggestion="The sim may be loading or paused in a menu; try again.",
        )

    selected = select_spots(
        data, gates_only=gates_only, min_radius_m=min_radius_m, include_service=include_service
    )
    gates = [s for s in data.spots if s["type"] in GATE_TYPES]
    jetway_gates = [s for s in gates if s["has_jetway"]]
    shown = selected[:limit]

    if not data.spots:
        summary = f"{icao}: scenérie nemá žádná stání."
    else:
        summary = (
            f"{icao}: {len(data.spots)} stání, z toho {len(gates)} gate "
            f"({len(jetway_gates)} s nástupním mostem). Vráceno {len(shown)} z {len(selected)} "
            f"po filtru."
        )
        if shown:
            top = shown[0]
            summary += f" Doporučené první: {top['name']} ({top['type']}, r {top['radius_m']} m)."
    if not data.complete:
        summary += " Data možná nejsou úplná (nedorazil konec odpovědi)."

    return ParkingSpotsResult(
        icao=icao,
        total_spots=len(data.spots),
        gates=len(gates),
        gates_with_jetway=len(jetway_gates),
        returned=len(shown),
        spots=[ParkingSpot(**s) for s in shown],
        summary_cs=summary,
    )
