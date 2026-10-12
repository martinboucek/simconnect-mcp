"""Airport parking spots via the SimConnect Facility Data API.

Local addition, not in upstream (see docs/simconnect-mcp.md in the parent
repo). Kept free of MCP so it can be tested on its own; the tool lives in
tools/parking.py.

Why a separate connection
=========================
The ``SimConnect.dll`` bundled with the Python-SimConnect package predates
the Facility Data API: it does not export ``SimConnect_AddToFacilityDefinition``
or ``SimConnect_RequestFacilityData`` at all. The DLL from the MSFS SDK does.
Rather than move the whole server -- and ``dispatch.py``'s carefully owned
dispatch loop -- onto a different DLL, this module opens its own short-lived
connection through the SDK DLL, asks for one airport, and closes again.
Both DLLs load side by side in one process (verified: distinct module
handles), and nothing here touches ``SimConnectManager``.

Wire format (measured live, MSFS 2024)
======================================
``SIMCONNECT_RECV_FACILITY_DATA`` is the 12-byte ``SIMCONNECT_RECV`` header
plus seven DWORDs (user request id, unique id, parent unique id, data type,
is-list-item, item index, list size) -- data starts at byte 40. The data
itself is the requested fields back to back with no padding, in definition
order. With ``PARKING_FIELDS`` below one TAXI_PARKING record is 44 bytes and
one JETWAY record is 4.

A JETWAY's ``PARKING_INDEX`` is the ``ItemIndex`` of the TAXI_PARKING it
serves (verified at LKPR: 27 jetways pointing at exactly the 27 gates).

An unknown ICAO gets a single ``SIMCONNECT_RECV_EXCEPTION`` and no
FACILITY_DATA_END, so the fetch loop must stop on an exception too.

Default MSFS 2024 scenery leaves ``N_AIRLINES`` at 0 everywhere measured
(LKPR, KATL, EGLL), so airline codes are not requested. Gate radii in
default scenery are often far smaller than the aircraft that really use
them (WBSB's gates are 5-14 m), so radius is reported, never used to hide
a spot unless the caller asks.
"""
from __future__ import annotations

import ctypes
import os
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RECV_ID_EXCEPTION = 1
RECV_ID_OPEN = 2
RECV_ID_FACILITY_DATA = 28
RECV_ID_FACILITY_DATA_END = 29

DATA_TYPE_AIRPORT = 0
DATA_TYPE_TAXI_PARKING = 15
DATA_TYPE_JETWAY = 18

PARKING_FIELDS = [
    "OPEN AIRPORT",
    "OPEN TAXI_PARKING",
    "TYPE", "TAXI_POINT_TYPE", "NAME", "SUFFIX", "NUMBER", "ORIENTATION",
    "HEADING", "RADIUS", "BIAS_X", "BIAS_Z", "N_AIRLINES",
    "CLOSE TAXI_PARKING",
    "OPEN JETWAY",
    "PARKING_INDEX",
    "CLOSE JETWAY",
    "CLOSE AIRPORT",
]

_HEADER = struct.Struct("<III")          # dwSize, dwVersion, dwID
_FACILITY_DATA = struct.Struct("<7I")    # follows the header
_PARKING = struct.Struct("<4iIi4fi")     # 44 bytes, see PARKING_FIELDS
_JETWAY = struct.Struct("<i")
_DATA_OFFSET = _HEADER.size + _FACILITY_DATA.size  # 40

# SimConnect SDK, TAXI_PARKING TYPE
PARKING_TYPES = {
    0: "NONE", 1: "RAMP_GA", 2: "RAMP_GA_SMALL", 3: "RAMP_GA_MEDIUM",
    4: "RAMP_GA_LARGE", 5: "RAMP_CARGO", 6: "RAMP_MIL_CARGO", 7: "RAMP_MIL_COMBAT",
    8: "GATE_SMALL", 9: "GATE_MEDIUM", 10: "GATE_HEAVY", 11: "DOCK_GA",
    12: "FUEL", 13: "VEHICLE", 14: "RAMP_GA_EXTRA", 15: "GATE_EXTRA",
}
# Not places to park an aircraft.
SERVICE_TYPES = {"FUEL", "VEHICLE"}
GATE_TYPES = {"GATE_SMALL", "GATE_MEDIUM", "GATE_HEAVY", "GATE_EXTRA"}

# SimConnect SDK, TAXI_PARKING NAME (SUFFIX uses the same values)
_NAME_LABELS = {
    0: "", 1: "PARKING", 2: "N PARKING", 3: "NE PARKING", 4: "E PARKING",
    5: "SE PARKING", 6: "S PARKING", 7: "SW PARKING", 8: "W PARKING",
    9: "NW PARKING", 10: "GATE", 11: "DOCK",
}
_NAME_LABELS.update({12 + i: f"GATE {chr(ord('A') + i)}" for i in range(26)})

DLL_ENV_VAR = "SIMCONNECT_SDK_DLL"
DLL_CANDIDATES = [
    r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll",
    r"C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll",
]


class ParkingError(Exception):
    """A fetch failed in a way the caller should report, with a code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def name_label(code: int) -> str:
    return _NAME_LABELS.get(code, f"NAME_{code}")


def spot_name(name: int, number: int, suffix: int) -> str:
    """Display name the way the sim's own parking list reads, e.g. ``GATE A 12``."""
    parts = [name_label(name), str(number)]
    if suffix:
        # GATE_A..GATE_Z as a suffix is just the letter; anything else is
        # appended as its label.
        parts.append(chr(ord("A") + suffix - 12) if 12 <= suffix <= 37 else name_label(suffix))
    return " ".join(p for p in parts if p)


@dataclass
class ParkingData:
    """Everything one airport request returned, before any filtering."""

    spots: list[dict[str, Any]] = field(default_factory=list)
    jetway_indexes: set[int] = field(default_factory=set)
    complete: bool = False
    exception: int | None = None


def parse_messages(messages: list[bytes]) -> ParkingData:
    """Decode raw SimConnect messages for one request into ParkingData.

    Pure function over bytes, so the committed wire fixtures can drive it.
    Message kinds it does not care about are skipped.
    """
    out = ParkingData()
    for raw in messages:
        _, _, recv_id = _HEADER.unpack_from(raw)
        if recv_id == RECV_ID_EXCEPTION:
            out.exception = struct.unpack_from("<I", raw, _HEADER.size)[0]
        elif recv_id == RECV_ID_FACILITY_DATA_END:
            out.complete = True
        elif recv_id == RECV_ID_FACILITY_DATA:
            _, _, _, data_type, is_list, item_index, _ = _FACILITY_DATA.unpack_from(
                raw, _HEADER.size
            )
            if data_type == DATA_TYPE_TAXI_PARKING and is_list:
                (ptype, _tpt, name, suffix, number, _orient,
                 heading, radius, bias_x, bias_z, _n_airlines) = _PARKING.unpack_from(
                    raw, _DATA_OFFSET
                )
                out.spots.append({
                    "index": item_index,
                    "name": spot_name(name, number, suffix),
                    "type": PARKING_TYPES.get(ptype, f"TYPE_{ptype}"),
                    "radius_m": round(radius, 1),
                    "heading_deg": round(heading % 360, 0),
                    "bias_x_m": round(bias_x, 1),
                    "bias_z_m": round(bias_z, 1),
                })
            elif data_type == DATA_TYPE_JETWAY and is_list:
                out.jetway_indexes.add(_JETWAY.unpack_from(raw, _DATA_OFFSET)[0])
    for spot in out.spots:
        spot["has_jetway"] = spot["index"] in out.jetway_indexes
    return out


def select_spots(
    data: ParkingData,
    *,
    gates_only: bool = False,
    min_radius_m: float | None = None,
    include_service: bool = False,
) -> list[dict[str, Any]]:
    """Filter and order spots: jetway gates first, then gates, then the rest,
    larger spots before smaller ones, sim order as the tie-breaker."""
    spots = [
        s for s in data.spots
        if (include_service or s["type"] not in SERVICE_TYPES)
        and (not gates_only or s["type"] in GATE_TYPES)
        and (min_radius_m is None or s["radius_m"] >= min_radius_m)
    ]
    return sorted(
        spots,
        key=lambda s: (
            not s["has_jetway"], s["type"] not in GATE_TYPES, -s["radius_m"], s["index"]
        ),
    )


def find_sdk_dll() -> str | None:
    """SDK SimConnect.dll path: $SIMCONNECT_SDK_DLL first, then the default SDK installs."""
    override = os.environ.get(DLL_ENV_VAR)
    if override:
        return override if Path(override).is_file() else None
    for candidate in DLL_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def fetch_parking(icao: str, dll_path: str, timeout_s: float = 8.0) -> ParkingData:
    """Blocking: open a private connection, request one airport, collect, close.

    Run it in an executor, never on the event loop.
    """
    from ctypes import POINTER, WINFUNCTYPE, c_char_p, c_void_p
    from ctypes.wintypes import DWORD, HANDLE, HWND

    try:
        dll = ctypes.WinDLL(dll_path)
        open_fn = dll.SimConnect_Open
        add_fn = dll.SimConnect_AddToFacilityDefinition
        request_fn = dll.SimConnect_RequestFacilityData
        dispatch_fn = dll.SimConnect_CallDispatch
        close_fn = dll.SimConnect_Close
    except (OSError, AttributeError) as exc:
        raise ParkingError("SDK_DLL_UNUSABLE", f"{dll_path}: {exc}") from exc

    handle = HANDLE()
    opened = open_fn(
        ctypes.byref(handle), b"simconnect-mcp-parking", HWND(0), DWORD(0), HANDLE(0), DWORD(0)
    )
    if opened != 0:
        raise ParkingError("NOT_CONNECTED", "SimConnect_Open failed -- is MSFS running?")

    messages: list[bytes] = []
    dispatch_proc = WINFUNCTYPE(None, POINTER(ctypes.c_ubyte), DWORD, c_void_p)

    def _collect(p_data, _cb_data, _context) -> None:
        size = _HEADER.unpack_from(bytes(p_data[:_HEADER.size]))[0]
        messages.append(bytes(p_data[:size]))

    callback = dispatch_proc(_collect)
    try:
        for name in PARKING_FIELDS:
            if add_fn(handle, DWORD(1), c_char_p(name.encode())) != 0:
                raise ParkingError("SDK_DLL_UNUSABLE", f"AddToFacilityDefinition rejected {name!r}")
        if request_fn(handle, DWORD(1), DWORD(1), c_char_p(icao.encode()), c_char_p(b"")) != 0:
            raise ParkingError("REQUEST_FAILED", f"RequestFacilityData failed for {icao}")
        return _pump(lambda: dispatch_fn(handle, callback, None), messages, timeout_s)
    finally:
        close_fn(handle)


def _pump(dispatch: Callable[[], Any], messages: list[bytes], timeout_s: float) -> ParkingData:
    deadline = time.monotonic() + timeout_s
    seen = 0
    while time.monotonic() < deadline:
        dispatch()
        new_ids = {_HEADER.unpack_from(m)[2] for m in messages[seen:]}
        seen = len(messages)
        if new_ids & {RECV_ID_FACILITY_DATA_END, RECV_ID_EXCEPTION}:
            break
        time.sleep(0.05)
    return parse_messages(messages)
