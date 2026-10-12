"""Tests for parking.py and the msfs_get_parking_spots tool.

The fixture holds real FACILITY_DATA wire messages captured from MSFS 2024
(see tests/fixtures/parking/facility_data.json): EFMA (small, GA only),
LKPR (27 jetway gates) and ZZZZ (unknown ICAO -> lone exception).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simconnect_mcp import parking
from simconnect_mcp.parking import (
    PARKING_FIELDS,
    ParkingData,
    ParkingError,
    parse_messages,
    select_spots,
    spot_name,
)
from simconnect_mcp.tools import parking as parking_tool
from simconnect_mcp.tools.models import ToolError
from simconnect_mcp.tools.parking import get_parking_spots

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "parking" / "facility_data.json").read_text()
)


def _messages(icao: str) -> list[bytes]:
    return [bytes.fromhex(m) for m in _FIXTURE["messages"][icao]]


def test_fixture_was_captured_with_current_definition():
    # A changed field list changes the record layout; recapture the fixture.
    assert _FIXTURE["fields"] == PARKING_FIELDS


def test_record_stride_is_packed():
    assert parking._PARKING.size == 44


def test_efma_small_airport():
    data = parse_messages(_messages("EFMA"))
    assert data.complete and data.exception is None
    assert len(data.spots) == 13
    assert not data.jetway_indexes
    first = data.spots[0]
    assert first["name"].startswith("PARKING")
    assert first["type"] == "RAMP_GA_MEDIUM"
    assert first["radius_m"] == 11.0
    assert 0 <= first["heading_deg"] < 360
    assert any(s["type"] == "FUEL" for s in data.spots)


def test_lkpr_jetways_map_to_gates():
    data = parse_messages(_messages("LKPR"))
    assert data.complete
    gates = [s for s in data.spots if s["type"].startswith("GATE")]
    jetway = [s for s in data.spots if s["has_jetway"]]
    assert len(data.jetway_indexes) == 27
    assert len(jetway) == 27
    # Every jetway points at a gate, never at a ramp.
    assert all(s["type"].startswith("GATE") for s in jetway)
    assert {s["name"] for s in gates[:3]} == {"GATE 5", "GATE 6", "GATE 7"}


def test_unknown_icao_is_an_exception_without_end():
    data = parse_messages(_messages("ZZZZ"))
    assert data.exception is not None
    assert not data.complete
    assert data.spots == []


@pytest.mark.parametrize("name,number,suffix,expected", [
    (10, 5, 0, "GATE 5"),
    (1, 12, 0, "PARKING 12"),
    (12, 3, 0, "GATE A 3"),
    (37, 1, 0, "GATE Z 1"),
    (10, 7, 13, "GATE 7 B"),
    (0, 4, 0, "4"),
    (99, 1, 0, "NAME_99 1"),
])
def test_spot_name(name, number, suffix, expected):
    assert spot_name(name, number, suffix) == expected


def _spot(index, type_, radius, jetway=False):
    return {"index": index, "name": f"S{index}", "type": type_, "radius_m": radius,
            "heading_deg": 0.0, "bias_x_m": 0.0, "bias_z_m": 0.0, "has_jetway": jetway}


def test_select_orders_jetway_gates_first_and_hides_service():
    data = ParkingData(spots=[
        _spot(0, "RAMP_GA_LARGE", 20),
        _spot(1, "FUEL", 30),
        _spot(2, "GATE_SMALL", 14),
        _spot(3, "GATE_SMALL", 18, jetway=True),
        _spot(4, "GATE_MEDIUM", 26),
    ])
    assert [s["index"] for s in select_spots(data)] == [3, 4, 2, 0]
    assert [s["index"] for s in select_spots(data, gates_only=True)] == [3, 4, 2]
    assert [s["index"] for s in select_spots(data, min_radius_m=18)] == [3, 4, 0]
    assert 1 in [s["index"] for s in select_spots(data, include_service=True)]


def test_find_sdk_dll_env_override(monkeypatch, tmp_path):
    dll = tmp_path / "SimConnect.dll"
    dll.write_bytes(b"")
    monkeypatch.setenv(parking.DLL_ENV_VAR, str(dll))
    assert parking.find_sdk_dll() == str(dll)
    monkeypatch.setenv(parking.DLL_ENV_VAR, str(tmp_path / "missing.dll"))
    assert parking.find_sdk_dll() is None


def test_find_sdk_dll_none_when_nothing_installed(monkeypatch):
    monkeypatch.delenv(parking.DLL_ENV_VAR, raising=False)
    monkeypatch.setattr(parking, "DLL_CANDIDATES", [r"Z:\nope\SimConnect.dll"])
    assert parking.find_sdk_dll() is None


def test_pump_stops_on_end_and_on_exception():
    end = _messages("EFMA")
    batches = iter([end[:5], end[5:]])
    collected: list[bytes] = []
    data = parking._pump(lambda: collected.extend(next(batches, [])), collected, 5.0)
    assert data.complete and len(data.spots) == 13

    collected = []
    data = parking._pump(lambda: collected.extend(_messages("ZZZZ")), collected, 5.0)
    assert data.exception is not None


# --- tool ---------------------------------------------------------------------

@pytest.fixture
def fake_fetch(monkeypatch):
    monkeypatch.setattr(parking_tool, "find_sdk_dll", lambda: r"C:\sdk\SimConnect.dll")

    def use(icao_messages=None, error=None):
        def fetch(icao, dll_path):
            if error:
                raise error
            return parse_messages(_messages(icao_messages or icao))
        monkeypatch.setattr(parking_tool, "fetch_parking", fetch)
    return use


async def test_tool_lists_lkpr_jetway_gates_first(fake_fetch):
    fake_fetch()
    result = await get_parking_spots("lkpr", limit=5)
    assert not isinstance(result, ToolError)
    assert result.icao == "LKPR"
    assert result.gates_with_jetway == 27
    assert result.returned == 5
    assert all(s.has_jetway for s in result.spots)
    assert "LKPR" in result.summary_cs


async def test_tool_unknown_icao(fake_fetch):
    fake_fetch()
    result = await get_parking_spots("ZZZZ")
    assert isinstance(result, ToolError)
    assert result.error == "AIRPORT_NOT_FOUND"


async def test_tool_without_sdk_dll(monkeypatch):
    monkeypatch.setattr(parking_tool, "find_sdk_dll", lambda: None)
    result = await get_parking_spots("WBSB")
    assert isinstance(result, ToolError)
    assert result.error == "SDK_DLL_NOT_FOUND"


async def test_tool_sim_not_running(fake_fetch):
    fake_fetch(error=ParkingError("NOT_CONNECTED", "SimConnect_Open failed"))
    result = await get_parking_spots("WBSB")
    assert isinstance(result, ToolError)
    assert result.error == "NOT_CONNECTED"
