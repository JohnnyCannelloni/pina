"""Tests for Ask Pina: POST /api/destinations/{id}/ask, and the pieces behind it.

No Ollama and no network. The LLM is a fake that returns a fixed SearchIntent
— which is the honest boundary: what the model says is not ours to test, but
everything we *do* with what it says is. The search runs for real, against
rows in the rolled-back test database.
"""

from collections.abc import Generator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import llm as llm_module
from app.geocode import GeocodeError, GeocodeResult
from app.llm import LlmError, LlmUnavailable, OllamaClient, clean_intent, get_llm
from app.main import app
from app.models import Category, Destination, OsmPlace
from app.schemas import Feature, SearchIntent

# Central Venice, well inside the seeded destination's box.
CENTER = (45.4408, 12.3155)


class FakeLlm:
    """Stands in for OllamaClient. Returns `intent`, or raises `error`."""

    def __init__(self) -> None:
        self.intent = SearchIntent()
        self.error: Exception | None = None
        self.calls: list[tuple[str, str, list[str]]] = []

    def parse(self, question: str, city: str, cuisines: list[str]) -> SearchIntent:
        self.calls.append((question, city, cuisines))
        if self.error is not None:
            raise self.error
        return self.intent


@pytest.fixture
def fake_llm() -> Generator[FakeLlm, None, None]:
    fake = FakeLlm()
    app.dependency_overrides[get_llm] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_llm, None)


def place(
    db: Session,
    ref: str,
    name: str,
    category: str = "restaurant",
    tags: dict | None = None,
    at: tuple[float, float] = CENTER,
) -> None:
    db.add(
        OsmPlace(
            source_ref=ref,
            name=name,
            category=category,
            latitude=at[0],
            longitude=at[1],
            region="nord-est",
            tags=tags,
        )
    )
    db.commit()


def ask(client: TestClient, destination: Destination, **body: object) -> dict:
    body.setdefault("question", "anything")
    response = client.post(f"/api/destinations/{destination.id}/ask", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def names(answer: dict) -> list[str]:
    return [result["name"] for result in answer["results"]]


# --- what ------------------------------------------------------------------


def test_cuisine_matches_by_tag_or_by_name(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    """Italian means tagged italian OR "trattoria" in the name.

    The name fallback is the point: two thirds of restaurants have no cuisine
    tag, so tag-only search would miss most of them.
    """
    place(db_session, "node/1", "Da Mario", tags={"cuisine": ["italian", "pizza"]})
    place(db_session, "node/2", "Trattoria alla Rampa")  # no tags at all
    place(db_session, "node/3", "Sushi Bar", tags={"cuisine": ["sushi"]})
    place(
        db_session, "node/4", "Caffè Italia", category="cafe",
        tags={"cuisine": ["italian"]},
    )
    fake_llm.intent = SearchIntent(
        category=Category.RESTAURANT, cuisines=["italian"], name_words=["trattoria"]
    )

    answer = ask(client, venice)

    assert sorted(names(answer)) == ["Da Mario", "Trattoria alla Rampa"]
    assert answer["total"] == 2
    assert answer["area_label"] == "in Venice"
    # The interpretation comes back for the UI's chips.
    assert answer["interpretation"]["cuisines"] == ["italian"]


def test_a_name_match_overrides_the_category_guess(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    """Measured live: "pastéis de nata" came back as category=restaurant, which
    hid the café named after them. A name that says it outright wins."""
    place(db_session, "node/1", "Fábrica de Pastéis de Nata", category="cafe",
          at=(CENTER[0] + 0.004, CENTER[1]))
    place(db_session, "node/2", "Tasca", tags={"cuisine": ["portuguese"]})
    place(db_session, "node/3", "Café Portugal", category="cafe",
          tags={"cuisine": ["portuguese"]})
    fake_llm.intent = SearchIntent(
        category=Category.RESTAURANT, cuisines=["portuguese"], name_words=["nata"]
    )

    answer = ask(client, venice)

    # The named café first despite being furthest; the tagged restaurant next.
    # A café with only the cuisine tag is still excluded by the category.
    assert names(answer) == ["Fábrica de Pastéis de Nata", "Tasca"]


def test_without_a_cuisine_name_words_rank_but_do_not_filter(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    """"a vegan café": the model's "vegano" must not hide vegan cafés with
    other names — measured live, it did."""
    place(db_session, "node/1", "Nicolau", category="cafe",
          tags={"diet:vegan": "yes"})
    place(db_session, "node/2", "Caffè Vegano", category="cafe",
          tags={"diet:vegan": "only"}, at=(CENTER[0] + 0.005, CENTER[1]))
    fake_llm.intent = SearchIntent(
        category=Category.CAFE, features=[Feature.VEGAN], name_words=["vegano"]
    )

    answer = ask(client, venice)

    # Both found; the name match ranks first even though it is further away.
    assert names(answer) == ["Caffè Vegano", "Nicolau"]


def test_every_feature_asked_for_must_be_present(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    place(db_session, "node/1", "Both", tags={
        "outdoor_seating": "yes", "diet:vegetarian": "yes"})
    place(db_session, "node/2", "Vegan Only Terrace", tags={
        "outdoor_seating": "yes", "diet:vegan": "only"})
    place(db_session, "node/3", "Terrace No Veg", tags={"outdoor_seating": "yes"})
    place(db_session, "node/4", "Veg Indoors", tags={
        "outdoor_seating": "no", "diet:vegetarian": "yes"})
    place(db_session, "node/5", "Unknown")
    fake_llm.intent = SearchIntent(
        features=[Feature.OUTDOOR_SEATING, Feature.VEGETARIAN]
    )

    answer = ask(client, venice)

    # A vegan-only place counts as vegetarian; "no" and missing do not count.
    assert sorted(names(answer)) == ["Both", "Vegan Only Terrace"]
    by_name = {result["name"]: result for result in answer["results"]}
    assert by_name["Both"]["details"]["features"] == ["outdoor_seating", "vegetarian"]


def test_features_nobody_has_are_dropped_and_reported(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    """Seafood exists, seafood with a recorded terrace does not: show the
    seafood, and say outdoor seating was dropped."""
    place(db_session, "node/1", "Fish", tags={"cuisine": ["seafood"]})
    place(db_session, "node/2", "Pizza Terrace", tags={
        "cuisine": ["pizza"], "outdoor_seating": "yes"})
    fake_llm.intent = SearchIntent(
        cuisines=["seafood"], features=[Feature.OUTDOOR_SEATING]
    )

    answer = ask(client, venice)

    assert names(answer) == ["Fish"]
    assert answer["dropped_features"] == ["outdoor_seating"]


def test_nothing_at_all_drops_nothing(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    fake_llm.intent = SearchIntent(
        cuisines=["seafood"], features=[Feature.OUTDOOR_SEATING]
    )

    answer = ask(client, venice)

    assert answer["results"] == []
    # Dropping the feature did not help, so it is not reported as dropped.
    assert answer["dropped_features"] == []


def test_an_empty_intent_returns_the_closest_places_capped(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    for index in range(30):
        place(db_session, f"node/{index}", f"Place {index}",
              at=(CENTER[0] + index * 0.0001, CENTER[1]))

    answer = ask(client, venice)

    assert answer["total"] == 30
    assert len(answer["results"]) == 25
    # Closest to the centre first.
    assert names(answer)[:3] == ["Place 0", "Place 1", "Place 2"]


def test_the_model_is_offered_only_cuisines_in_this_destination(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    place(db_session, "node/1", "A", tags={"cuisine": ["italian"]})
    place(db_session, "node/2", "B", tags={"cuisine": ["italian", "seafood"]})
    # Far outside Venice: its cuisine must not be offered.
    place(db_session, "node/3", "C", tags={"cuisine": ["thai"]}, at=(38.7, -9.1))

    ask(client, venice, question="fish please")

    question, city, cuisines = fake_llm.calls[0]
    assert (question, city) == ("fish please", "Venice")
    assert cuisines == ["italian", "seafood"]  # commonest first


def test_saved_places_are_marked(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    place(db_session, "node/1", "Saved One")
    client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "Saved One", "category": "restaurant",
              "source": "osm", "source_ref": "node/1"},
    )

    answer = ask(client, venice)

    assert answer["results"][0]["already_saved"] is True


# --- where -----------------------------------------------------------------


def test_a_named_area_narrows_the_search(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    rialto = (45.4380, 12.3358)
    place(db_session, "node/1", "By The Bridge", at=rialto)
    place(db_session, "node/2", "Two Streets Away", at=(rialto[0] + 0.003, rialto[1]))
    place(db_session, "node/3", "Across Town", at=(rialto[0] - 0.03, rialto[1]))
    fake_llm.intent = SearchIntent(area="Rialto")
    fake_geocoder.results = [  # type: ignore[attr-defined]
        GeocodeResult(
            display_name="Ponte di Rialto, Venezia", name="Ponte di Rialto",
            latitude=rialto[0], longitude=rialto[1],
            # A bridge's own outline is tiny; the box must still take in the
            # streets around it.
            south=rialto[0] - 0.0002, west=rialto[1] - 0.0002,
            north=rialto[0] + 0.0002, east=rialto[1] + 0.0002,
            osm_ref="way/1", country_code="it",
        )
    ]

    answer = ask(client, venice)

    assert names(answer) == ["By The Bridge", "Two Streets Away"]
    assert answer["area_label"] == "near Rialto"
    assert answer["area_found"] is True
    # Looked up inside Venice only.
    assert fake_geocoder.queries == ["Rialto"]  # type: ignore[attr-defined]
    south, west, north, east = fake_geocoder.viewboxes[0]  # type: ignore[attr-defined]
    assert south < venice.south and north > venice.north


def test_the_city_under_another_name_searches_the_whole_city(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    """"pizza in Venice" when the destination is called Venezia."""
    place(db_session, "node/1", "Edge Of Town",
          at=(venice.south + 0.001, venice.west + 0.001))
    fake_llm.intent = SearchIntent(area="Venice")
    fake_geocoder.results = [  # type: ignore[attr-defined]
        GeocodeResult(
            display_name="Venezia, Veneto, Italia", name="Venezia",
            latitude=CENTER[0], longitude=CENTER[1],
            south=venice.south, west=venice.west,
            north=venice.north, east=venice.east,
            osm_ref="relation/44741", country_code="it",
        )
    ]

    answer = ask(client, venice)

    assert names(answer) == ["Edge Of Town"]
    assert answer["area_label"] == "in Venice"


def test_the_most_important_matching_area_wins(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    """Measured in real Venice: Nominatim's first "Rialto" is a service road
    on the mainland; the market everyone means comes fourth."""
    market = (45.4396, 12.3345)
    place(db_session, "node/1", "By The Market", at=market)
    place(db_session, "node/2", "By The Road", at=(45.4720, 12.2113))
    fake_llm.intent = SearchIntent(area="Rialto")

    def result(name: str, at: tuple[float, float], importance: float) -> GeocodeResult:
        return GeocodeResult(
            display_name=name, name=name, latitude=at[0], longitude=at[1],
            south=at[0] - 0.0001, west=at[1] - 0.0001,
            north=at[0] + 0.0001, east=at[1] + 0.0001,
            osm_ref=f"node/{name}", country_code="it", importance=importance,
        )

    fake_geocoder.results = [  # type: ignore[attr-defined]
        result("Rialto road", (45.4720, 12.2113), 0.04),
        result("Mercato di Rialto", market, 0.17),
    ]

    assert names(ask(client, venice)) == ["By The Market"]


@pytest.mark.parametrize("failure", ["no results", "geocoder down"])
def test_an_area_that_cannot_be_found_widens_and_says_so(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object, failure: str,
) -> None:
    place(db_session, "node/1", "Somewhere")
    fake_llm.intent = SearchIntent(area="Atlantis")
    if failure == "geocoder down":
        fake_geocoder.error = GeocodeError("down")  # type: ignore[attr-defined]

    answer = ask(client, venice)

    assert names(answer) == ["Somewhere"]
    assert answer["area_found"] is False
    assert answer["area_label"] == "in Venice"


def test_no_area_uses_the_map_view_when_given(
    client: TestClient, db_session: Session, venice: Destination,
    fake_llm: FakeLlm, fake_geocoder: object,
) -> None:
    place(db_session, "node/1", "In View", at=(45.4400, 12.3300))
    place(db_session, "node/2", "Out Of View", at=(45.4300, 12.3000))
    viewport = {"south": 45.438, "west": 12.328, "north": 45.442, "east": 12.332}

    answer = ask(client, venice, viewport=viewport)

    assert names(answer) == ["In View"]
    assert answer["area_label"] == "in the map view"
    assert fake_geocoder.queries == []  # type: ignore[attr-defined]


# --- failures ----------------------------------------------------------------


def test_ollama_not_running_is_a_503_with_the_fix(
    client: TestClient, venice: Destination, fake_llm: FakeLlm,
) -> None:
    fake_llm.error = LlmUnavailable("Start it with: brew services start ollama")

    response = client.post(
        f"/api/destinations/{venice.id}/ask", json={"question": "pizza"}
    )

    assert response.status_code == 503
    assert "brew services start ollama" in response.json()["detail"]


def test_an_unreadable_answer_is_a_502(
    client: TestClient, venice: Destination, fake_llm: FakeLlm,
) -> None:
    fake_llm.error = LlmError("The assistant gave an answer Pina couldn't read.")

    response = client.post(
        f"/api/destinations/{venice.id}/ask", json={"question": "pizza"}
    )

    assert response.status_code == 502


def test_a_blank_question_is_a_422(client: TestClient, venice: Destination) -> None:
    response = client.post(
        f"/api/destinations/{venice.id}/ask", json={"question": "   "}
    )
    assert response.status_code == 422


def test_unknown_destination_is_a_404(client: TestClient, fake_llm: FakeLlm) -> None:
    response = client.post("/api/destinations/999999/ask", json={"question": "x"})
    assert response.status_code == 404


# --- cleaning what the model says ----------------------------------------------


def test_clean_intent_keeps_only_what_the_data_can_answer() -> None:
    raw = SearchIntent(
        category=Category.RESTAURANT,
        cuisines=["italian", "klingon", "italian"],
        name_words=[
            "Trattoria", "restaurant", "ok", "trattoria", "osteria", "venice"
        ],
        area="Venice",
    )

    cleaned = clean_intent(raw, city="Venice", cuisines=["italian", "pizza"])

    assert cleaned.cuisines == ["italian"]  # invented and duplicate dropped
    # Lowercased, de-duplicated; generic, too-short and place-name words dropped.
    assert cleaned.name_words == ["trattoria", "osteria"]
    # Naming the city itself is not an area within it.
    assert cleaned.area is None


def test_clean_intent_drops_the_area_from_name_words() -> None:
    raw = SearchIntent(name_words=["museu", "belém"], area="Belém")
    cleaned = clean_intent(raw, city="Lisboa", cuisines=[])
    assert cleaned.name_words == ["museu"]
    assert cleaned.area == "Belém"


class _FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> object:
        return self._payload


def test_ollama_client_reads_a_structured_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict = {}

    def fake_post(url: str, json: dict, timeout: float) -> _FakeResponse:
        sent.update(json)
        content = (
            '{"category": "restaurant", "cuisines": ["italian"], '
            '"features": ["outdoor_seating"], "name_words": ["trattoria"], '
            '"area": "Alfama"}'
        )
        return _FakeResponse(200, {"message": {"content": content}})

    monkeypatch.setattr(llm_module.httpx, "post", fake_post)

    intent = OllamaClient().parse("terrace italian in Alfama", "Lisbon", ["italian"])

    assert intent == SearchIntent(
        category=Category.RESTAURANT, cuisines=["italian"],
        features=[Feature.OUTDOOR_SEATING], name_words=["trattoria"], area="Alfama",
    )
    # The constraints are actually sent: no thinking pass, and a schema whose
    # cuisine enum is this destination's list.
    assert sent["think"] is False
    assert sent["format"]["properties"]["cuisines"]["items"]["enum"] == ["italian"]


def test_ollama_client_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm_module.httpx, "post",
        lambda *a, **k: _FakeResponse(200, {"message": {"content": "not json"}}),
    )
    with pytest.raises(LlmError):
        OllamaClient().parse("x", "Lisbon", [])


def test_ollama_client_reports_it_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_module.httpx, "post", refuse)
    with pytest.raises(LlmUnavailable, match="brew services start ollama"):
        OllamaClient().parse("x", "Lisbon", [])


def test_ollama_client_reports_a_missing_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_module.httpx, "post", lambda *a, **k: _FakeResponse(404, {})
    )
    with pytest.raises(LlmUnavailable, match="ollama pull"):
        OllamaClient().parse("x", "Lisbon", [])


# --- details on the other endpoints ------------------------------------------


def test_nearby_and_saved_places_carry_details(
    client: TestClient, db_session: Session, venice: Destination,
) -> None:
    place(db_session, "node/1", "Da Mario", tags={
        "cuisine": ["italian"], "opening_hours": "Mo-Su 12:00-23:00",
        "website": "https://damario.it", "phone": "+39 041 1"})

    nearby = client.get(
        f"/api/destinations/{venice.id}/nearby",
        params={"south": 45.43, "west": 12.30, "north": 45.45, "east": 12.33},
    ).json()
    assert nearby["places"][0]["details"]["cuisine"] == ["italian"]

    saved = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "Da Mario", "category": "restaurant",
              "source": "osm", "source_ref": "node/1"},
    ).json()
    assert saved["details"]["website"] == "https://damario.it"

    client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "My Aunt's", "category": "restaurant"},
    )
    listed = {
        p["name"]: p["details"]
        for p in client.get(f"/api/destinations/{venice.id}/places").json()
    }
    assert listed["Da Mario"]["opening_hours"] == "Mo-Su 12:00-23:00"
    assert listed["My Aunt's"] is None
