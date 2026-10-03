"""Tests for the set_sim_time tool."""

from __future__ import annotations

import pytest

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


@pytest.mark.asyncio
async def test_set_sim_time_sends_events(mock_simconnect):
    mock_simconnect["simvar_values"].update(
        {"ZULU_TIME": 75000.0, "LOCAL_TIME": 60600.0}
    )
    result = await set_sim_time(10, 30, local=True)
    assert result["status"] == "ok"
    assert result["sent_zulu"] == "14:30"
    names = [c.args[0] for c in mock_simconnect["ae"].find.call_args_list]
    assert names == ["ZULU_HOURS_SET", "ZULU_MINUTES_SET"]
    sent = [c.args[0] for c in mock_simconnect["event"].call_args_list]
    assert sent == [14, 30]


@pytest.mark.asyncio
async def test_set_sim_time_optional_date(mock_simconnect):
    mock_simconnect["simvar_values"].update(
        {"ZULU_TIME": 75000.0, "LOCAL_TIME": 60600.0}
    )
    await set_sim_time(12, 0, local=False, day_of_year=300, year=2026)
    names = [c.args[0] for c in mock_simconnect["ae"].find.call_args_list]
    assert names == [
        "ZULU_YEARS_SET", "ZULU_DAYS_SET", "ZULU_HOURS_SET", "ZULU_MINUTES_SET",
    ]


@pytest.mark.asyncio
async def test_set_sim_time_clock_unreadable(mock_simconnect):
    result = await set_sim_time(10, 0)
    assert result["status"] == "error"
    mock_simconnect["event"].assert_not_called()
