from unittest.mock import MagicMock

from app.port_allocator import NoPortsAvailableError, allocate_port


def _mock_db(used_ports):
    db = MagicMock()
    db.query.return_value.all.return_value = [MagicMock(ui_port=p) for p in used_ports]
    return db


def test_allocates_lowest_free_port(monkeypatch):
    monkeypatch.setattr("app.port_allocator.settings.ui_port_range_start", 9000)
    monkeypatch.setattr("app.port_allocator.settings.ui_port_range_end", 9002)

    assert allocate_port(_mock_db([9000])) == 9001


def test_raises_when_range_exhausted(monkeypatch):
    monkeypatch.setattr("app.port_allocator.settings.ui_port_range_start", 9000)
    monkeypatch.setattr("app.port_allocator.settings.ui_port_range_end", 9001)

    try:
        allocate_port(_mock_db([9000, 9001]))
        assert False, "expected NoPortsAvailableError"
    except NoPortsAvailableError:
        pass
