"""the overpass client: query building, parsing, retries and limits.

no network: httpx.post is replaced with a scripted fake, and the retry sleep
is patched out.
"""

import httpx
import pytest

from app import overpass as overpass_module
from app.overpass import (
    AMENITY_CATEGORIES,
    MAX_SPAN_DEGREES,
    TOURISM_CATEGORIES,
    OverpassClient,
    OverpassError,
    build_query,
    parse_elements,
)

# a box in central lisbon: (south, west, north, east).
LISBON = (38.69, -9.23, 38.80, -9.09)


def node(osm_id: int, tags: dict, lat: float = 38.7, lon: float = -9.1) -> dict:
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags}


def way(osm_id: int, tags: dict, lat: float = 38.7, lon: float = -9.2) -> dict:
    return {"type": "way", "id": osm_id, "center": {"lat": lat, "lon": lon}, "tags": tags}


# --- the query ---------------------------------------------------------------


def test_the_query_asks_for_every_category_in_the_box() -> None:
    query = build_query(*LISBON, timeout=90)

    assert query.startswith("[out:json][timeout:90];")
    assert "out center tags;" in query
    # nodes, ways and relations, named, in south,west,north,east order.
    assert query.count('nwr["') == 2
    assert query.count('["name"](38.69,-9.23,38.8,-9.09)') == 2
    # the value filters come from the category dicts.
    for value in list(AMENITY_CATEGORIES) + list(TOURISM_CATEGORIES):
        assert value in query


# --- parsing -----------------------------------------------------------------


def test_nodes_ways_and_relations_become_rows() -> None:
    rows = parse_elements(
        [
            node(1, {"name": "Café A", "amenity": "cafe"}, lat=38.71, lon=-9.14),
            way(2, {"name": "Museu B", "tourism": "museum"}, lat=38.69, lon=-9.21),
            {
                "type": "relation",
                "id": 3,
                "center": {"lat": 38.72, "lon": -9.13},
                "tags": {"name": "Gallery C", "tourism": "gallery"},
            },
        ]
    )

    assert [(r["source_ref"], r["category"], r["latitude"], r["longitude"]) for r in rows] == [
        ("node/1", "cafe", 38.71, -9.14),
        # ways and relations use their centre point.
        ("way/2", "sight", 38.69, -9.21),
        ("relation/3", "sight", 38.72, -9.13),
    ]


def test_a_node_and_a_way_with_the_same_id_are_different_places() -> None:
    rows = parse_elements(
        [
            node(7, {"name": "Kiosk", "amenity": "cafe"}),
            way(7, {"name": "Museum", "tourism": "museum"}),
        ]
    )

    assert {r["source_ref"] for r in rows} == {"node/7", "way/7"}


def test_unusable_elements_are_dropped() -> None:
    rows = parse_elements(
        [
            node(1, {"amenity": "restaurant"}),  # no name
            node(2, {"name": "A Bench", "amenity": "bench"}),  # unmapped
            {"type": "way", "id": 3, "tags": {"name": "No Centre", "tourism": "museum"}},
            {"type": "area", "id": 4, "tags": {"name": "Odd", "amenity": "cafe"}},
            node(5, {"name": "Kept", "amenity": "fast_food"}),
        ]
    )

    assert [r["name"] for r in rows] == ["Kept"]


def test_amenity_wins_over_tourism() -> None:
    rows = parse_elements(
        [node(1, {"name": "Museum Café", "amenity": "cafe", "tourism": "museum"})]
    )

    assert rows[0]["category"] == "cafe"


def test_tags_are_kept_and_cleaned() -> None:
    rows = parse_elements(
        [
            node(1, {
                "name": "Trattoria Rosa", "amenity": "restaurant",
                "cuisine": "Italian; pizza", "outdoor_seating": "yes",
                "contact:website": "www.rosa.it",
                "contact:phone": "+39 041 123;+39 041 456",
                "toilets": "yes",
            }),
            node(2, {"name": "Sneaky", "amenity": "cafe", "website": "javascript:alert(1)"}),
        ]
    )

    assert rows[0]["tags"] == {
        "cuisine": ["italian", "pizza"],
        "outdoor_seating": "yes",
        "website": "https://www.rosa.it",
        "phone": "+39 041 123",
    }
    # the unsafe link is dropped, leaving no tags.
    assert rows[1]["tags"] is None


# --- fetching ----------------------------------------------------------------


class _Response:
    def __init__(self, status: int, payload: object = None, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):
    """replace httpx.post with a queue of responses; record calls and sleeps."""

    class Script:
        def __init__(self) -> None:
            self.responses: list[object] = []
            self.calls: list[dict] = []
            self.sleeps: list[float] = []

    script = Script()

    def fake_post(url: str, **kwargs: object) -> object:
        script.calls.append({"url": url, **kwargs})
        response = script.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(overpass_module.httpx, "post", fake_post)
    monkeypatch.setattr(overpass_module, "sleep", script.sleeps.append)
    return script


def ok(elements: list[dict]) -> _Response:
    return _Response(200, {"elements": elements})


def test_a_successful_fetch(scripted) -> None:
    scripted.responses = [ok([node(1, {"name": "A", "amenity": "cafe"})])]

    elements = OverpassClient().fetch(*LISBON)

    assert len(elements) == 1
    call = scripted.calls[0]
    assert call["url"] == overpass_module.settings.OVERPASS_URL
    assert "out center tags;" in call["data"]["data"]
    assert call["headers"]["User-Agent"].startswith("Pina/")
    # a little longer than the query's own timeout.
    assert call["timeout"] > overpass_module.settings.OVERPASS_TIMEOUT_SECONDS


def test_429_is_retried_with_backoff(scripted) -> None:
    scripted.responses = [_Response(429), ok([])]

    assert OverpassClient().fetch(*LISBON) == []
    assert len(scripted.calls) == 2
    assert scripted.sleeps == [2.0]


def test_504_is_retried_and_retry_after_is_honoured(scripted) -> None:
    scripted.responses = [_Response(504, headers={"Retry-After": "7"}), ok([])]

    OverpassClient().fetch(*LISBON)

    assert scripted.sleeps == [7.0]


@pytest.mark.parametrize("status", [502, 503])
def test_proxy_errors_are_retried(scripted, status: int) -> None:
    scripted.responses = [_Response(status), ok([])]

    assert OverpassClient().fetch(*LISBON) == []
    assert len(scripted.calls) == 2


def test_three_busy_answers_give_up_with_a_readable_error(scripted) -> None:
    scripted.responses = [_Response(429), _Response(429), _Response(429)]

    with pytest.raises(OverpassError, match="busy"):
        OverpassClient().fetch(*LISBON)
    assert len(scripted.calls) == 3
    assert scripted.sleeps == [2.0, 4.0]


def test_other_http_errors_are_not_retried(scripted) -> None:
    scripted.responses = [_Response(400)]

    with pytest.raises(OverpassError, match="HTTP 400"):
        OverpassClient().fetch(*LISBON)
    assert len(scripted.calls) == 1


def test_a_server_side_timeout_with_http_200_is_a_failure(scripted) -> None:
    scripted.responses = [
        _Response(200, {
            "elements": [node(1, {"name": "Partial", "amenity": "cafe"})],
            "remark": 'runtime error: Query timed out in "query" at line 3 after 91 seconds.',
        })
    ]

    with pytest.raises(OverpassError, match="couldn't finish"):
        OverpassClient().fetch(*LISBON)


def test_a_connection_error_is_readable(scripted) -> None:
    scripted.responses = [httpx.ConnectError("refused")]

    with pytest.raises(OverpassError, match="Could not reach"):
        OverpassClient().fetch(*LISBON)


def test_unreadable_json_is_an_error(scripted) -> None:
    scripted.responses = [_Response(200, ValueError("not json"))]

    with pytest.raises(OverpassError, match="couldn't read"):
        OverpassClient().fetch(*LISBON)


def test_an_oversized_box_is_rejected_without_a_request(scripted) -> None:
    south, west = 38.0, -10.0
    too_big = (south, west, south + MAX_SPAN_DEGREES + 0.1, west + 0.1)

    with pytest.raises(OverpassError, match="too large"):
        OverpassClient().fetch(*too_big)
    assert scripted.calls == []
