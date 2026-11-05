"""The backup lifetime rules: when a lecture needs a backup and when a backup may be torn down.

Defaults: a capture agent (CA) must be healthy for 24h before its backup goes, and the
backup stays for 4 days after the last lecture that needed it.
"""

import asyncio
from datetime import timedelta

import pytest

from app import audit, decision_engine
from app.models import AgentHealth, AuditActorType, AuditEvent, AuditOutcome, Instance, InstanceStatus, utcnow

BackupReason = decision_engine.BackupReason


@pytest.fixture
def engine(engine_db, monkeypatch):
    """Run passes with Docker and the backups' PyCA UIs stubbed out; returns what was started/stopped."""
    calls = {"started": [], "stopped": [], "pyca": None}

    async def pyca_activity(instance):
        return calls["pyca"]

    monkeypatch.setattr(decision_engine, "provision_and_start", lambda i: calls["started"].append(i.venue))
    monkeypatch.setattr(decision_engine.compose_manager, "down", lambda i: calls["stopped"].append(i.venue))
    monkeypatch.setattr(decision_engine, "pyca_activity", pyca_activity)

    def run(oc):
        asyncio.run(decision_engine.run_once(oc))
        return calls

    run.calls = calls
    return run


def _status(db, venue="room-1"):
    db.expire_all()
    return db.query(Instance).filter_by(venue=venue).one().status


# ---- observing agents ---------------------------------------------------------------


def test_healthy_run_starts_on_first_healthy_sighting_and_survives_further_polls(db, agent_state):
    first = utcnow()
    health = decision_engine.observe_agent(db, agent_state(), first)
    assert (health.healthy_since, health.last_unhealthy_at) == (first, None)

    health = decision_engine.observe_agent(db, agent_state(), first + timedelta(seconds=60))
    assert health.healthy_since == first


def test_any_unhealthy_sighting_ends_the_healthy_run(db, agent_state):
    now = utcnow()
    decision_engine.observe_agent(db, agent_state(), now)
    for state in ("offline", "error", "unknown"):
        later = now + timedelta(minutes=1)
        health = decision_engine.observe_agent(db, agent_state(state=state), later)
        assert (health.healthy_since, health.last_unhealthy_at) == (None, later)

    # Not reporting in counts too, whatever the last state said.
    health = decision_engine.observe_agent(db, agent_state(seconds_since_update=10_000), later)
    assert health.healthy_since is None


def test_uploading_counts_as_healthy(db, agent_state):
    health = decision_engine.observe_agent(db, agent_state(state="uploading"), utcnow())
    assert health.healthy_since is not None and health.last_unhealthy_at is None


def test_a_gap_in_observations_restarts_the_healthy_run(db, agent_state):
    now = utcnow()
    decision_engine.observe_agent(db, agent_state(), now - timedelta(hours=10))
    # Nothing seen for 10h (controller down): can't vouch for the agent in between.
    health = decision_engine.observe_agent(db, agent_state(), now)
    assert health.healthy_since == now


# ---- starting -------------------------------------------------------------------------


def test_lecture_needs_a_backup_when_the_ca_is_down_now(agent_state, scheduled_event):
    event = scheduled_event(minutes_to_start=20 * 60)  # even a day ahead
    health = AgentHealth(agent_id="room-1")
    reason = decision_engine._backup_reason
    assert (
        reason(event, agent_state(state="offline", seconds_since_update=1e9), health)
        == BackupReason.agent_offline
    )
    assert reason(event, agent_state(state="error"), health) == BackupReason.agent_unhealthy


def test_lecture_still_needs_a_backup_if_the_ca_was_down_within_24h_before_it(agent_state, scheduled_event):
    # Fixed 2h ago; the lecture is in 3h - the CA was down within 24h of it.
    health = AgentHealth(agent_id="room-1", last_unhealthy_at=utcnow() - timedelta(hours=2))
    event = scheduled_event(minutes_to_start=3 * 60)
    assert decision_engine._backup_reason(event, agent_state(), health) == BackupReason.recently_unhealthy


def test_lecture_does_not_need_a_backup_if_the_ca_was_down_longer_than_24h_before_it(
    agent_state, scheduled_event
):
    # Down 10h ago, but the lecture is 20h away: 30h before it, outside the window.
    health = AgentHealth(agent_id="room-1", last_unhealthy_at=utcnow() - timedelta(hours=10))
    event = scheduled_event(minutes_to_start=20 * 60)
    assert decision_engine._backup_reason(event, agent_state(), health) is None
    # And a CA never seen down needs nothing.
    assert decision_engine._backup_reason(event, agent_state(), AgentHealth(agent_id="room-1")) is None


def test_fixed_ca_still_gets_a_backup_started_and_the_lecture_is_remembered(
    engine, engine_db, fake_opencast, agent_state, scheduled_event, seed_health
):
    seed_health(unhealthy_ago=timedelta(hours=1))
    event = scheduled_event(minutes_to_start=60)

    calls = engine(fake_opencast([event], {"room-1": agent_state()}))

    assert calls["started"] == ["room-1"]
    assert _status(engine_db) == InstanceStatus.running
    health = engine_db.get(AgentHealth, "room-1")
    assert health.last_needed_event_end_at == decision_engine._naive_utc(event.end)


# ---- tearing down ---------------------------------------------------------------------


def test_ca_coming_online_during_the_recording_does_not_stop_the_backup(
    engine, engine_db, make_instance, fake_opencast, agent_state, offline_agent, scheduled_event
):
    """The headline To-do case: the backup is recording when the CA comes back."""
    event = scheduled_event(minutes_to_start=-10)  # started 10 minutes ago
    engine(fake_opencast([event], {"room-1": offline_agent()}))
    assert _status(engine_db) == InstanceStatus.running

    # The CA reports in healthy again while the backup's PyCA is mid-recording.
    engine.calls["pyca"] = BackupReason.recording_in_progress
    for _ in range(3):
        calls = engine(fake_opencast([event], {"room-1": agent_state()}))

    assert calls["stopped"] == []
    assert _status(engine_db) == InstanceStatus.running


def test_backup_stays_until_the_ca_has_been_healthy_for_24h(
    engine, engine_db, make_instance, fake_opencast, agent_state, seed_health
):
    make_instance(status=InstanceStatus.running)
    seed_health(healthy_for=timedelta(hours=23), unhealthy_ago=timedelta(hours=23))

    assert engine(fake_opencast([], {"room-1": agent_state()}))["stopped"] == []
    assert _status(engine_db) == InstanceStatus.running


def test_backup_stays_for_4_days_after_the_last_lecture_that_needed_it(
    engine, engine_db, make_instance, fake_opencast, agent_state, seed_health
):
    make_instance(status=InstanceStatus.running)
    seed_health(healthy_for=timedelta(days=3), needed_lecture_ended_ago=timedelta(days=3, hours=12))

    assert engine(fake_opencast([], {"room-1": agent_state()}))["stopped"] == []
    assert _status(engine_db) == InstanceStatus.running


def test_backup_is_torn_down_once_the_ca_is_stable_and_4_days_have_passed(
    engine, engine_db, make_instance, fake_opencast, agent_state, seed_health
):
    instance_id = make_instance(status=InstanceStatus.running).id
    seed_health(healthy_for=timedelta(days=5), needed_lecture_ended_ago=timedelta(days=4, hours=1))

    assert engine(fake_opencast([], {"room-1": agent_state()}))["stopped"] == ["room-1"]
    assert _status(engine_db) == InstanceStatus.stopped
    stop = engine_db.query(AuditEvent).filter_by(instance_id=instance_id).one()
    assert stop.detail["reason"] == "no_longer_needed"
    assert stop.detail["last_needed_lecture_end"]


@pytest.mark.parametrize(
    "activity",
    [BackupReason.recording_in_progress, BackupReason.ingest_in_progress, BackupReason.pyca_unreachable],
)
def test_backup_is_never_torn_down_while_its_pyca_holds_work(
    engine, engine_db, make_instance, fake_opencast, agent_state, seed_health, activity
):
    instance_id = make_instance(status=InstanceStatus.running).id
    seed_health(healthy_for=timedelta(days=5))
    engine.calls["pyca"] = activity

    assert engine(fake_opencast([], {"room-1": agent_state()}))["stopped"] == []
    deferred = engine_db.query(AuditEvent).filter_by(instance_id=instance_id).one()
    assert deferred.detail["reason"] == activity.value


def test_a_ca_going_down_again_restarts_the_24h_clock(
    engine, engine_db, make_instance, fake_opencast, agent_state, offline_agent, seed_health
):
    make_instance(status=InstanceStatus.running)
    seed_health(healthy_for=timedelta(days=5))

    engine(fake_opencast([], {"room-1": offline_agent()}))  # down again, briefly
    calls = engine(fake_opencast([], {"room-1": agent_state()}))  # healthy for seconds only

    assert calls["stopped"] == []
    assert _status(engine_db) == InstanceStatus.running


def test_no_decision_without_the_agents_current_state(
    engine, engine_db, make_instance, fake_opencast, seed_health
):
    make_instance(status=InstanceStatus.running)
    seed_health(healthy_for=timedelta(days=5))

    # Opencast doesn't report the agent this pass: keep the backup rather than guess.
    assert engine(fake_opencast([], {}))["stopped"] == []
    assert _status(engine_db) == InstanceStatus.running


# ---- review fixes (PR #3) ---------------------------------------------------------------


def test_agents_are_observed_with_no_lecture_or_backup_so_a_later_lecture_sees_the_outage(
    engine, engine_db, fake_opencast, agent_state, offline_agent, scheduled_event, monkeypatch
):
    """An outage before a lecture enters the schedule window still counts towards it - here
    with a stability window (48h) longer than the one-day lookahead."""
    monkeypatch.setattr(decision_engine.settings, "agent_stable_hours", 48)

    # Down, with nothing scheduled and no backup: previously this agent wasn't even looked at.
    engine(fake_opencast([], {"room-1": offline_agent()}))
    assert engine_db.get(AgentHealth, "room-1").last_unhealthy_at is not None
    assert engine_db.query(Instance).count() == 0

    # Recovered, and now a lecture 20h away enters the window: it still needs a backup.
    calls = engine(fake_opencast([scheduled_event(minutes_to_start=20 * 60)], {"room-1": agent_state()}))
    assert calls["started"] == ["room-1"]


def test_all_agent_states_come_from_one_request_per_pass(
    engine, fake_opencast, offline_agent, scheduled_event
):
    venues = [f"room-{n}" for n in range(5)]
    oc = fake_opencast([scheduled_event(v) for v in venues], {v: offline_agent(v) for v in venues})

    calls = engine(oc)

    assert oc.agent_list_calls == 1
    assert sorted(calls["started"]) == venues


def test_failing_to_fetch_the_agent_list_fails_the_pass_and_touches_nothing(
    engine, engine_db, make_instance, fake_opencast, scheduled_event, seed_health
):
    make_instance(status=InstanceStatus.running)
    seed_health(healthy_for=timedelta(days=5))

    with pytest.raises(decision_engine.OpencastError):
        engine(fake_opencast([scheduled_event()], decision_engine.OpencastError("agents.json: HTTP 500")))

    assert engine.calls["started"] == engine.calls["stopped"] == []
    assert _status(engine_db) == InstanceStatus.running


class _CrashMidStart(BaseException):
    """Stands in for the process dying while Docker is starting the backup."""


def test_ui_credentials_are_saved_before_docker_is_started(
    engine_db, monkeypatch, fake_opencast, offline_agent, scheduled_event
):
    def crash(instance):
        raise _CrashMidStart

    monkeypatch.setattr(decision_engine, "provision_and_start", crash)

    with pytest.raises(_CrashMidStart):
        asyncio.run(decision_engine.run_once(fake_opencast([scheduled_event()], {"room-1": offline_agent()})))

    engine_db.rollback()  # drop anything not committed before the "crash"
    instance = engine_db.query(Instance).one()
    assert instance.status == InstanceStatus.starting
    assert instance.ui_username and instance.ui_password


def test_backup_without_stored_credentials_is_treated_as_busy(make_instance):
    instance = make_instance(status=InstanceStatus.running)  # no ui_username/ui_password

    assert asyncio.run(decision_engine.pyca_activity(instance)) == BackupReason.pyca_unreachable


# ---- what holds a backup up (only active work, and a hand-start) -------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({"recording"}, BackupReason.recording_in_progress),
        ({"finished recording"}, BackupReason.ingest_in_progress),  # ingest queued
        ({"uploading", "paused after recording"}, BackupReason.ingest_in_progress),
        # Waiting for an operator, or finished: safe on disk, nothing to interrupt.
        ({"paused after recording"}, None),
        ({"partial recording", "failed uploading", "failed recording"}, None),
        ({"finished uploading", "upcoming"}, None),
        (set(), None),
    ],
)
def test_only_active_recording_or_ingest_holds_a_backup_up(statuses, expected):
    assert decision_engine.activity_from_statuses(statuses) == expected


def test_backup_holding_un_ingested_recordings_is_still_torn_down(
    engine, engine_db, make_instance, fake_opencast, agent_state, seed_health
):
    """Duplicates nobody ingests must not keep a backup up forever - the recordings stay on disk."""
    make_instance(status=InstanceStatus.running)
    seed_health(healthy_for=timedelta(days=5))
    engine.calls["pyca"] = decision_engine.activity_from_statuses({"paused after recording"})

    assert engine(fake_opencast([], {"room-1": agent_state()}))["stopped"] == ["room-1"]


def test_backup_started_by_hand_stays_up_until_stopped_by_hand(
    engine, engine_db, make_instance, fake_opencast, agent_state, seed_health
):
    instance_id = make_instance(status=InstanceStatus.running).id
    seed_health(healthy_for=timedelta(days=5))  # otherwise due for teardown
    operator = audit.Actor(type=AuditActorType.operator, source_ip="10.0.0.1")
    engine_db.add(
        AuditEvent(
            actor_type=AuditActorType.system,
            actor="system",
            action=audit.INSTANCE_START,
            outcome=AuditOutcome.success,
            instance_id=instance_id,
        )
    )
    audit.record(
        engine_db, actor=operator, action=audit.INSTANCE_START, instance=engine_db.get(Instance, instance_id)
    )
    engine_db.commit()

    for _ in range(3):
        assert engine(fake_opencast([], {"room-1": agent_state()}))["stopped"] == []
    assert _status(engine_db) == InstanceStatus.running

    # Once anyone stops it, the hold is over; a later automatic start isn't held.
    for actor in (operator, audit.SYSTEM):
        action = audit.INSTANCE_STOP if actor is operator else audit.INSTANCE_START
        audit.record(engine_db, actor=actor, action=action, instance=engine_db.get(Instance, instance_id))
    engine_db.commit()
    assert not audit.started_by_operator(engine_db, instance_id)
    assert engine(fake_opencast([], {"room-1": agent_state()}))["stopped"] == ["room-1"]


def test_a_failed_manual_start_does_not_hold(db, make_instance):
    instance = make_instance(status=InstanceStatus.error)
    operator = audit.Actor(type=AuditActorType.operator)
    audit.record(
        db, actor=operator, action=audit.INSTANCE_START, outcome=AuditOutcome.failure, instance=instance
    )
    db.commit()

    assert not audit.started_by_operator(db, instance.id)


def test_instance_list_shows_a_manual_hold(client, db, make_instance):
    instance = make_instance(status=InstanceStatus.running)
    audit.record(
        db, actor=audit.Actor(type=AuditActorType.operator), action=audit.INSTANCE_START, instance=instance
    )
    db.commit()

    (listed,) = client.get("/instances").json()
    assert listed["started_by_operator"] is True
