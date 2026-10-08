from app.opencast_client import _extract_rtsp_source


def test_single_capability_serialised_as_bare_object():
    # Real example from a live agent with exactly one capability - Opencast
    # omits the array wrapper in this case.
    capabilities = {"item": {"key": "capture.device.names", "value": "defaults"}}
    assert _extract_rtsp_source(capabilities) is None


def test_single_matching_capability_as_bare_object():
    capabilities = {"item": {"key": "capture.device.presenter.src", "value": "rtsp://x"}}
    assert _extract_rtsp_source(capabilities) == "rtsp://x"


def test_multiple_capabilities_as_list():
    capabilities = {
        "item": [
            {"key": "capture.device.names", "value": "presenter"},
            {"key": "capture.device.presenter.src", "value": "rtsp://y"},
        ]
    }
    assert _extract_rtsp_source(capabilities) == "rtsp://y"


def test_no_capabilities():
    assert _extract_rtsp_source(None) is None
    assert _extract_rtsp_source({}) is None
