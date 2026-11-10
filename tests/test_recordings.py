"""Recording discovery, the retention clean-up, and the Recordings API/page.

Recordings are built on a temporary disk laid out like the real thing:
<recordings_dir>/<venue>/recording-<start>-<id>/, and PyCA's database at
<instances_dir>/<venue>/data/pyca.db.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import audit, compose_manager, instance_lifecycle, pyca_config, recordings
from app.config import settings
from app.models import AuditEvent, RecordingRetention, utcnow

# PyCA status codes used below (see recordings.PYCA_STATUS).
RECORDING, FAILED_RECORDING, UPLOADING, FINISHED_UPLOADING, PAUSED = 2, 3, 5, 7, 9


@pytest.fixture
def disk(tmp_path, monkeypatch):
    """Point INSTANCES_DIR and RECORDINGS_DIR at temp dirs.

    Returns add(venue, days_ago, status=..., size=...), which creates one recording.
    """
    monkeypatch.setattr(settings, "instances_dir", tmp_path / "instances")
    monkeypatch.setattr(settings, "recordings_dir", tmp_path / "recordings")
    monkeypatch.setattr(settings, "recording_retention_days", 7)
    counter = {"n": 0}

    def add(venue="room-1", days_ago=1.0, status=FINISHED_UPLOADING, size=1000):
        counter["n"] += 1
        start = int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp())
        mp = f"mp-{counter['n']}"
        name = f"recording-{start}-{mp}"
        folder = tmp_path / "recordings" / venue / name
        folder.mkdir(parents=True)
        (folder / f"{name}.mp4").write_bytes(b"x" * size)
        if status is not None:
            state = tmp_path / "instances" / venue / "data"
            state.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(state / "pyca.db") as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS recorded_event "
                    '(uid TEXT, start INTEGER, "end" INTEGER, status INTEGER)'
                )
                conn.execute(
                    "INSERT INTO recorded_event VALUES (?, ?, ?, ?)", (mp, start, start + 60, status)
                )
        return name

    add.root = tmp_path / "recordings"
    return add


def _names(recs):
    return sorted(r.name for r in recs)


# ---- discovery ------------------------------------------------------------------------


def test_recordings_are_read_from_disk_with_pyca_status(db, disk):
    ingested = disk("room-1", days_ago=2, status=FINISHED_UPLOADING, size=1234)
    waiting = disk("room-2", days_ago=1, status=PAUSED)
    no_db_row = disk("room-3", days_ago=1, status=None)
    (disk.root / "room-1" / "not-a-recording").mkdir()

    recs = {r.name: r for r in recordings.list_recordings(db)}

    assert sorted(recs) == sorted([ingested, waiting, no_db_row])
    first = recs[ingested]
    assert (first.venue, first.mediapackage_id, first.size_bytes) == ("room-1", "mp-1", 1234)
    assert (first.pyca_status, first.retention_days, first.retention_overridden) == (
        "finished uploading",
        7,
        False,
    )
    assert first.delete_after == first.started_at + timedelta(days=7)
    assert recs[waiting].pyca_status == "paused after recording"
    assert recs[no_db_row].pyca_status == "unknown"
    assert _names(recordings.list_recordings(db, venue="room-2")) == [waiting]


def test_missing_instances_dir_means_no_recordings(db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "recordings_dir", tmp_path / "nope")
    assert recordings.list_recordings(db) == []


# ---- clean-up -------------------------------------------------------------------------


def test_cleanup_deletes_past_retention_whether_ingested_or_not(db, disk):
    old_ingested = disk(days_ago=8, status=FINISHED_UPLOADING)
    old_not_ingested = disk(days_ago=8, status=PAUSED)
    old_unknown = disk(days_ago=30, status=None)
    old_failed = disk(days_ago=9, status=FAILED_RECORDING)
    recent = disk(days_ago=6, status=PAUSED)

    deleted = recordings.clean_up(db)

    assert _names(deleted) == sorted([old_ingested, old_not_ingested, old_unknown, old_failed])
    assert _names(recordings.list_recordings(db)) == [recent]
    assert not (disk.root / "room-1" / old_not_ingested).exists()


def test_cleanup_never_deletes_a_recording_still_being_written_or_uploaded(db, disk):
    recording = disk(days_ago=8, status=RECORDING)
    uploading = disk(days_ago=8, status=UPLOADING)

    assert recordings.clean_up(db) == []
    assert _names(recordings.list_recordings(db)) == sorted([recording, uploading])


def test_cleanup_honours_per_recording_retention(db, disk):
    kept_longer = disk(days_ago=8)
    deleted_sooner = disk(days_ago=3)
    for name, days in ((kept_longer, 30), (deleted_sooner, 2)):
        recordings.set_retention(db, recordings.find_recording(db, "room-1", name), days)
    db.commit()

    assert _names(recordings.clean_up(db)) == [deleted_sooner]
    assert _names(recordings.list_recordings(db)) == [kept_longer]
    # The override goes with the recording it belonged to.
    assert [r.name for r in db.query(RecordingRetention)] == [kept_longer]


def test_each_deletion_is_audited(db, disk):
    name = disk(days_ago=8, status=PAUSED, size=500)

    recordings.clean_up(db)

    (event,) = db.query(AuditEvent).all()
    assert (event.action, event.actor, event.venue) == (audit.RECORDING_DELETED, "system", "room-1")
    assert event.detail["recording"] == name
    assert event.detail["pyca_status"] == "paused after recording"
    assert (event.detail["retention_days"], event.detail["size_bytes"]) == (7, 500)


def test_cleanup_uses_the_configured_default(db, disk, monkeypatch):
    disk(days_ago=4)
    monkeypatch.setattr(settings, "recording_retention_days", 3)

    assert len(recordings.clean_up(db, now=utcnow())) == 1


# ---- API ------------------------------------------------------------------------------


def test_api_lists_recordings_and_settings(client, disk):
    name = disk("room-1", days_ago=1, status=PAUSED)
    disk("room-2", days_ago=2)

    body = client.get("/recordings").json()
    assert [r["venue"] for r in body] == ["room-1", "room-2"]  # newest first
    assert body[0]["name"] == name
    assert (body[0]["pyca_status"], body[0]["in_progress"], body[0]["retention_days"]) == (
        "paused after recording",
        False,
        7,
    )
    assert [r["venue"] for r in client.get("/recordings", params={"venue": "room-2"}).json()] == ["room-2"]
    assert client.get("/recordings/settings").json()["default_retention_days"] == 7


def test_api_changes_retention_and_audits_it(client, db, disk):
    name = disk(days_ago=1)

    resp = client.put(f"/recordings/room-1/{name}/retention", json={"retention_days": 30})
    assert resp.status_code == 200
    assert (resp.json()["retention_days"], resp.json()["retention_overridden"]) == (30, True)

    resp = client.put(f"/recordings/room-1/{name}/retention", json={"retention_days": 14})
    assert resp.json()["retention_days"] == 14

    changes = (
        db.query(AuditEvent).filter_by(action=audit.RECORDING_RETENTION_CHANGED).order_by(AuditEvent.id).all()
    )
    assert [(e.actor_type.value, e.detail["from"], e.detail["to"]) for e in changes] == [
        ("operator", "default (7)", 30),
        ("operator", 30, 14),
    ]


@pytest.mark.parametrize("days", [0, -1, 3651, None])  # None: there is no "reset to default"
def test_api_rejects_out_of_range_retention(client, disk, days):
    name = disk(days_ago=1)
    assert (
        client.put(f"/recordings/room-1/{name}/retention", json={"retention_days": days}).status_code == 422
    )


@pytest.mark.parametrize(
    ("venue", "name"),
    [
        ("room-1", "recording-1-missing"),
        ("room-9", "recording-1-mp-1"),
        ("room-1", "..%2F..%2Fetc"),
        ("..", "recording-1-mp-1"),
    ],
)
def test_api_404s_for_unknown_or_unsafe_recordings(client, disk, venue, name):
    disk(days_ago=1)
    assert client.put(f"/recordings/{venue}/{name}/retention", json={"retention_days": 30}).status_code == 404


def test_recordings_page_is_linked_from_the_navbar(client):
    page = client.get("/library")
    assert page.status_code == 200
    assert "/static/recordings.js" in page.text
    assert 'href="/library"' in client.get("/").text


# ---- manual delete --------------------------------------------------------------------


def test_api_deletes_a_recording_now_and_audits_it_as_manual(client, db, disk):
    name = disk(days_ago=1, status=PAUSED, size=300)  # recent and not ingested: deletable on request
    client.put(f"/recordings/room-1/{name}/retention", json={"retention_days": 30})

    assert client.delete(f"/recordings/room-1/{name}").status_code == 204

    assert recordings.list_recordings(db) == []
    assert not (disk.root / "room-1" / name).exists()
    assert db.query(RecordingRetention).count() == 0  # its setting went with it
    event = db.query(AuditEvent).filter_by(action=audit.RECORDING_DELETED).one()
    assert (event.actor_type.value, event.detail["trigger"], event.detail["recording"]) == (
        "operator",
        "manual",
        name,
    )


def test_api_refuses_to_delete_a_recording_in_progress(client, db, disk):
    name = disk(days_ago=1, status=RECORDING)

    resp = client.delete(f"/recordings/room-1/{name}")

    assert resp.status_code == 409
    assert "still recording" in resp.json()["detail"]
    assert [r.name for r in recordings.list_recordings(db)] == [name]


@pytest.mark.parametrize(("venue", "name"), [("room-1", "recording-1-missing"), ("..", "recording-1-mp-1")])
def test_api_delete_404s_for_unknown_or_unsafe_recordings(client, disk, venue, name):
    disk(days_ago=1)
    assert client.delete(f"/recordings/{venue}/{name}").status_code == 404


def test_cleanup_deletions_are_marked_as_retention(db, disk):
    disk(days_ago=8)
    recordings.clean_up(db)
    assert db.query(AuditEvent).one().detail["trigger"] == "retention"


# ---- where PyCA writes recordings -------------------------------------------------------


def test_each_pyca_stack_writes_recordings_to_the_venues_folder(make_instance, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "instances_dir", tmp_path / "instances")
    monkeypatch.setattr(settings, "recordings_dir", tmp_path / "recordings")
    instance = make_instance(venue="lt1")

    compose = compose_manager.render_compose_file(instance).read_text()

    mount = f'"{tmp_path / "recordings" / "lt1"}:/var/lib/pyca/recordings"'
    assert compose.count(mount) == 5  # every PyCA service sees the same recordings
    assert compose.count("./data:/var/lib/pyca\n") == 5  # PyCA's own state stays with the instance


def test_recordings_are_fragmented_mp4(make_instance):
    conf = pyca_config.render_pyca_conf(make_instance(rtsp_source="rtsp://cam/s"), "admin", "pw")

    assert "-movflags +frag_keyframe+empty_moov {{dir}}/{{name}}.mp4" in conf
    assert "files            = '{{dir}}/{{name}}.mp4'" in conf
    assert ".mkv" not in conf
    assert "-c:v copy" in conf  # still not re-encoded


def test_recordings_folder_is_created_and_handed_to_pycas_user_when_root(
    make_instance, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(instance_lifecycle.os, "geteuid", lambda: 0)
    chowned = []
    monkeypatch.setattr(instance_lifecycle.os, "chown", lambda path, uid, gid: chowned.append((path, uid)))

    path = instance_lifecycle.prepare_recordings_dir(make_instance(venue="lt1"))

    assert path == tmp_path / "lt1" and path.is_dir()
    assert chowned == [(path, settings.pyca_uid)]


def test_data_folder_is_created_and_handed_to_pycas_user_when_root(make_instance, monkeypatch, tmp_path):
    # PyCA can't create its database in a root-owned folder, so every service exits.
    monkeypatch.setattr(settings, "instances_dir", tmp_path)
    monkeypatch.setattr(instance_lifecycle.os, "geteuid", lambda: 0)
    chowned = []
    monkeypatch.setattr(instance_lifecycle.os, "chown", lambda path, uid, gid: chowned.append((path, uid)))

    path = instance_lifecycle.prepare_data_dir(make_instance(venue="lt1"))

    assert path == settings.instance_dir("lt1") / "data" and path.is_dir()
    assert chowned == [(path, settings.pyca_uid)]


def test_backups_trust_the_configured_ca_bundle(make_instance, monkeypatch, tmp_path):
    # The PyCA image's CA bundle is old; without this, an HTTPS Opencast can be unreachable.
    monkeypatch.setattr(settings, "instances_dir", tmp_path)
    monkeypatch.setattr(settings, "pyca_ca_bundle", Path("/etc/ssl/certs/ca-certificates.crt"))
    instance = make_instance(venue="lt1", rtsp_source="rtsp://cam/s")

    compose = compose_manager.render_compose_file(instance).read_text()
    conf = pyca_config.render_pyca_conf(instance, "admin", "pw")

    assert compose.count('"/etc/ssl/certs/ca-certificates.crt:/etc/pyca/ca-bundle.pem:ro"') == 5
    assert "certificate      = '/etc/pyca/ca-bundle.pem'" in conf


def test_backups_use_pycas_own_ca_bundle_by_default(make_instance, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "instances_dir", tmp_path)
    instance = make_instance(venue="lt1", rtsp_source="rtsp://cam/s")

    compose = compose_manager.render_compose_file(instance).read_text()
    conf = pyca_config.render_pyca_conf(instance, "admin", "pw")

    assert "ca-bundle" not in compose
    assert "certificate      = ''" in conf
