from unittest.mock import MagicMock

import pytest

from app import instance_lifecycle
from app.config import settings
from app.instance_lifecycle import apply_port_map
from app.port_allocator import NoPortsAvailableError, PortMapError, allocate_port, load_port_map


def _mock_db(used_ports):
    db = MagicMock()
    db.query.return_value.all.return_value = [MagicMock(ui_port=p) for p in used_ports]
    return db


@pytest.fixture
def port_map(monkeypatch, tmp_path):
    """Write the port map file; with no call, there is none."""
    path = tmp_path / "pyca-ports.yaml"
    monkeypatch.setattr(settings, "pyca_ports_file", path)

    def write(text: str) -> None:
        path.write_text(text)

    return write


@pytest.fixture(autouse=True)
def _small_range(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ui_port_range_start", 9000)
    monkeypatch.setattr(settings, "ui_port_range_end", 9002)
    monkeypatch.setattr(settings, "pyca_ports_file", tmp_path / "absent.yaml")


def test_allocates_lowest_free_port():
    assert allocate_port(_mock_db([9000]), "lt1") == 9001


def test_raises_when_range_exhausted():
    with pytest.raises(NoPortsAvailableError):
        allocate_port(_mock_db([9000, 9001, 9002]), "lt1")


# ---- the port map file ---------------------------------------------------------------


def test_a_mapped_venue_gets_its_port(port_map):
    port_map("lt1: 9050\n")

    assert allocate_port(_mock_db([]), "lt1") == 9050


def test_unmapped_venues_skip_ports_reserved_for_mapped_ones(port_map):
    port_map("lt1: 9000\nlt2: 9001\n")

    assert allocate_port(_mock_db([]), "lt9") == 9002


def test_no_file_means_no_mapping():
    assert load_port_map() == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("lt1: 9001\nlt2: 9001\n", "given to both 'lt1' and 'lt2'"),
        ("lt1: ninety\n", "must be a number"),
        ("lt1: 70000\n", "must be a number"),
        ("lt1: true\n", "must be a number"),
        ("- lt1\n- lt2\n", "must map venue names to ports"),
        ("lt1: [9001\n", "isn't valid YAML"),
    ],
)
def test_a_broken_port_map_is_rejected_with_the_reason(port_map, text, message):
    port_map(text)

    with pytest.raises(PortMapError, match=message):
        load_port_map()


# ---- existing instances move at their next start -------------------------------------


def test_an_existing_instance_moves_to_its_mapped_port(db, make_instance, port_map):
    instance = make_instance(venue="lt1")  # on 9000, from before the map
    port_map("lt1: 9016\n")

    apply_port_map(db, instance)

    assert instance.ui_port == 9016


def test_an_unmapped_instance_moves_off_a_port_reserved_for_another_venue(db, make_instance, port_map):
    squatter = make_instance(venue="lt9")  # 9000
    port_map("lt1: 9000\n")

    apply_port_map(db, squatter)

    assert squatter.ui_port == 9001


def test_an_unmapped_instance_on_an_unreserved_port_stays(db, make_instance, port_map):
    instance = make_instance(venue="lt9")
    port_map("lt1: 9016\n")

    apply_port_map(db, instance)

    assert instance.ui_port == 9000


def test_a_mapped_port_held_by_another_instance_doesnt_stop_the_start(db, make_instance, port_map):
    make_instance(venue="lt9")  # holds 9000
    instance = make_instance(venue="lt1")  # 9001
    port_map("lt1: 9000\n")

    apply_port_map(db, instance)  # no error: the backup still starts, on its old port

    assert instance.ui_port == 9001


def test_a_new_venue_whose_mapped_port_is_taken_still_gets_a_port(db, make_instance, port_map):
    make_instance(venue="lt9")  # holds 9000
    port_map("lt1: 9000\n")

    instance = instance_lifecycle.build_instance(db, "lt1", "lt1", "rtsp://cam/s")

    assert instance.ui_port == 9001


def test_new_instances_are_built_on_their_mapped_port(db, port_map):
    port_map("lt1: 9016\n")

    instance = instance_lifecycle.build_instance(db, "lt1", "lt1", "rtsp://cam/s")

    assert instance.ui_port == 9016
