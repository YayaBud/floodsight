"""Regression for FS-19/§I: one server-authored event clock.

The Annamayya chronology was reconstructed from data/evidence/*.json (EVD-01
through EVD-28) after finding the code's old T=0 (05:45, overtopping) and the
frontend's separate "03:15" narrative both disagreed with the sourced 03:30
Pincha failure time and the MHA-reported washout time. T=0 is now anchored at
the dam failure/washout event (06:30 IST, MHA D692 point value) per an
explicit user decision -- these tests pin that decision down.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.scenarios import get_scenario_manifest, load_event_clock

IST = timezone(timedelta(hours=5, minutes=30))


def test_event_clock_absent_scenarios_report_not_available():
    # OUT-OF-SCOPE-NON-INDIAN: "derna" dropped — commented out of SCENARIOS.
    for key in ("rishiganga", "phutkal", "south_lhonak"):
        clock = load_event_clock(key)
        assert clock["classification"] == "NOT_AVAILABLE"
        assert clock["origin_iso"] is None
        assert clock["timeline_events"] == []


def test_annamayya_origin_is_the_washout_event_not_overtopping():
    """T=0 must be the dam failure/washout (EVD-17, MHA point value 06:30),
    not the overtopping-initiation window the code used to hardcode (05:45)."""
    clock = load_event_clock("annamayya")
    assert clock["origin_anchor"] == "dam_failure_washout"
    origin = datetime.fromisoformat(clock["origin_iso"])
    assert origin == datetime(2021, 11, 19, 6, 30, 0, tzinfo=IST)


def test_pincha_failure_precedes_washout_by_the_sourced_interval():
    """EVD-04 (03:30 IST, OBSERVED) must produce t_s consistent with the new
    T=0 -- NOT the old, now-invalid, T-150 label (which was computed against
    a different, since-abandoned T=0 candidate)."""
    clock = load_event_clock("annamayya")
    events = {e["id"]: e for e in clock["timeline_events"]}
    pincha = events["pincha_failure"]
    assert pincha["classification"] == "OBSERVED"
    assert pincha["t_s"] == -10800.0  # 03:30 to 06:30 = 180 min, not 150


def test_overtopping_precedes_washout_as_a_separate_event():
    """Overtopping initiation (EVD-16) must exist as its own timeline event
    distinct from T=0, not be collapsed into the washout anchor."""
    clock = load_event_clock("annamayya")
    events = {e["id"]: e for e in clock["timeline_events"]}
    assert "overtopping_initiation" in events
    assert events["overtopping_initiation"]["t_s"] < 0
    assert events["washout"]["t_s"] == 0.0


def test_pre_washout_arrivals_are_explained_not_hidden():
    """EVD-21/22 (gorge exit, Togurupeta) are sourced at 06:15-06:25, before
    the 06:30 washout -- these must carry an explanatory note that they are
    pre-washout overtopping-flow arrivals, per the user's explicit physical
    interpretation, not silently negative numbers with no context."""
    clock = load_event_clock("annamayya")
    events = {e["id"]: e for e in clock["timeline_events"]}
    for eid in ("gorge_exit_overtopping_flow", "togurupeta_overtopping_flow"):
        assert events[eid]["t_s"] < 0
        assert "overtopping" in events[eid]["note"].lower()


def test_every_timeline_event_carries_provenance():
    clock = load_event_clock("annamayya")
    for event in clock["timeline_events"]:
        assert event.get("classification") in {"OBSERVED", "OFFICIAL_ESTIMATE", "MODEL_RECONSTRUCTION", "ASSUMED"}
        assert event.get("source")
        assert "t_s" in event


def test_manifest_exposes_the_same_clock():
    manifest = get_scenario_manifest("annamayya")
    assert manifest["event_clock"]["origin_anchor"] == "dam_failure_washout"
    assert len(manifest["event_clock"]["timeline_events"]) >= 10
