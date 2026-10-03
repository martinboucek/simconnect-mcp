"""Tests for the set_sim_time tool."""

from __future__ import annotations

import pytest

from simconnect_mcp.tools.models import ToolError
from simconnect_mcp.tools.simtime import compute_target, set_sim_time


def test_zulu_passthrough():
    t = compute_target(21, 15, False, 75000, 60600)
    assert (t["zulu_hour"], t["zulu_minute"], t["day_shift"]) == (21, 15, 0)


def test_local_uses_sim_offset():
    # Caribbean: zulu 20:50, local 16:50 -> UTC-4. Local 10:00 is 14:00Z.
    t = compute_target(10, 0, True, 75000, 60600)
    assert t["utc_offset_min"] == -240
    assert (t["zulu_hour"], t["zulu_minute"]) == (14, 0)
    assert t["day_shift"] == 0


def test_local_crossing_midnight_reports_day_shift():
    # Local 22:30 at UTC-4 is 02:30Z the next day.
    t = compute_target(22, 30, True, 75000, 60600)
    assert (t["zulu_hour"], t["zulu_minute"]) == (2, 30)
    assert t["day_shift"] == 1


def test_offset_when_clocks_straddle_midnight():
    # Zulu 00:50, local 20:50 (previous day) is still UTC-4.
    t = compute_target(12, 0, True, 3000, 75000)
    assert t["utc_offset_min"] == -240


@pytest.mark.parametrize("hour,minute", [(24, 0), (-1, 0), (10, 60), (10, -1)])
def test_range_validation(hour, minute):
    with pytest.raises(ValueError):
        compute_target(hour, minute, False, 0, 0)


@pytest.fixture
def clock(mock_simconnect):
    """Sim clock at 20:50Z, local 16:50 (UTC-4), day 276; events land instantly."""
    values = mock_simconnect["simvar_values"]
    values.update({"ZULU_TIME": 75000.0, "LOCAL_TIME": 60600.0, "ZULU_DAY_OF_YEAR": 276.0})
    return mock_simconnect


def _sent_events(mock):
    return [c.args[0] for c in mock["ae"].find.call_args_list]


async def test_local_time_converted_and_events_sent(clock):
    result = await set_sim_time(10, 30, local=True)
    assert not isinstance(result, ToolError)
    assert result.sent_zulu == "14:30"
    assert result.utc_offset_min == -240
    assert _sent_events(clock) == ["ZULU_HOURS_SET", "ZULU_MINUTES_SET"]


async def test_not_verified_when_sim_clock_does_not_move(clock):
    # The mocked sim never applies the events, so the read-back still says 20:50.
    result = await set_sim_time(14, 30, local=False)
    assert result.verified is False
    assert result.read_back_zulu == "20:50"
    assert "20:50" in result.warning


async def test_verified_when_read_back_matches(clock):
    clock["simvar_values"]["ZULU_TIME"] = 14 * 3600 + 30 * 60.0
    result = await set_sim_time(14, 30, local=False)
    assert result.verified is True
    assert result.warning is None


async def test_optional_date_sent_first(clock):
    await set_sim_time(12, 0, local=False, day_of_year=300, year=2026)
    assert _sent_events(clock) == [
        "ZULU_YEAR_SET", "ZULU_DAY_SET", "ZULU_HOURS_SET", "ZULU_MINUTES_SET",
    ]


async def test_wrong_day_read_back_is_not_verified(clock):
    clock["simvar_values"]["ZULU_TIME"] = 12 * 3600.0
    result = await set_sim_time(12, 0, local=False, day_of_year=300)
    # The mocked sim still reads day 276, so the date change is flagged.
    assert result.verified is False
    assert "day 276" in result.warning


async def test_midnight_crossing_warns(clock):
    result = await set_sim_time(22, 30, local=True)
    assert result.sent_zulu == "02:30"
    assert "midnight" in result.warning


async def test_clock_unreadable_changes_nothing(mock_simconnect):
    result = await set_sim_time(10, 0)
    assert isinstance(result, ToolError)
    assert result.error == "CLOCK_UNAVAILABLE"
    mock_simconnect["event"].assert_not_called()
