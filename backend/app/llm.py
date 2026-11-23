"""the llm side of ask pina: turn a question into a SearchIntent via ollama.

the model never names places. it fills in a search, and app/ask.py runs it
against osm_places. ollama's structured outputs (`format`, a json schema)
constrain decoding, so `features` and `cuisines` can only be values the data
can answer. this is the only file that talks to the model.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

import httpx
from pydantic import ValidationError

from app.config import settings
from app.models import Category
from app.schemas import Feature, SearchIntent

logger = logging.getLogger(__name__)

#: words that describe every place of a category; dropped from name_words so
#: "cafe with wifi" doesn't require "cafe" in the name.
GENERIC_NAME_WORDS = frozenset(
    {
        "restaurant", "restaurante", "ristorante", "cafe", "café", "caffè",
        "coffee", "bar", "food", "place", "places", "eat", "eatery", "shop",
    }
)

MAX_NAME_WORDS = 5


class LlmError(RuntimeError):
    """the model answered, but not with something usable."""


class LlmUnavailable(LlmError):
    """ollama isn't running or the model isn't pulled. maps to a 503 with the fix."""


class Llm(Protocol):
    """what app/ask.py needs from a model. tests use a fake."""

    def parse(
        self,
        question: str,
        city: str,
        cuisines: list[str],
        previous: SearchIntent | None = None,
    ) -> SearchIntent: ...


SYSTEM_PROMPT = """\
You turn a traveller's request into a search over a map of {city}.
Fill in the JSON fields:

- category: "restaurant", "cafe" (cafés, coffee, pastries, cakes, desserts, \
ice cream, bakeries) or \
"sight" (museums, viewpoints, attractions). null if the request does not say.
- cuisines: ONLY when the request names a food or cuisine, pick the matching \
values from the allowed list. "pasta" -> "italian", "fish" -> "seafood". \
Never add a cuisine the request did not ask for: "a café" alone has no \
cuisine. Empty if none.
- features: only ones the request explicitly asks for. "terrace" or \
"outside" -> outdoor_seating. "veggie" -> vegetarian. "vegan" -> vegan \
(not vegetarian). Empty if none.
- name_words: up to 3 lowercase words likely to appear IN THE NAME of a \
matching place, in the local language too. For Italian food: "trattoria", \
"pizzeria", "osteria". For museums in Lisbon: "museu", "museum". Never \
generic words like restaurant, cafe or bar. Empty if unsure.
- area: the neighbourhood, street or landmark the request names, exactly as \
written ("Alfama", "near the Colosseum" -> "Colosseum"). null if the request \
names no area, or only names {city} itself.

Never invent places. You only describe the search."""

#: added for follow-ups. without the examples, small models tend to drop the
#: rest of the search ("only vegan ones" -> vegan anything, anywhere).
FOLLOW_UP_PROMPT = """

This message follows an earlier one. The current search is:
{previous}

If the message refines the current search, return the complete updated search and keep every field it does not change:
- "only vegan ones" -> add the vegan feature, keep the rest
- "what about Baixa?" -> change only the area
- "cafés instead" -> change only the category
- "without the terrace" -> remove that feature
If it asks for something unrelated, start a new search."""


def _schema(cuisines: list[str]) -> dict[str, object]:
    """the json schema ollama must follow, with this destination's cuisines.

    hand-written and flat (no $refs), since the cuisine enum is per request
    and small models follow flat schemas more reliably.
    """
    cuisine_items: dict[str, object] = (
        {"type": "string", "enum": cuisines} if cuisines else {"type": "string"}
    )
    return {
        "type": "object",
        "properties": {
            "category": {
                "enum": [str(category) for category in Category] + [None]
            },
            "cuisines": {
                "type": "array",
                "items": cuisine_items,
                # no cuisine data means nothing to choose from.
                **({} if cuisines else {"maxItems": 0}),
            },
            "features": {
                "type": "array",
                "items": {"enum": [str(feature) for feature in Feature]},
            },
            "name_words": {"type": "array", "items": {"type": "string"}},
            "area": {"type": ["string", "null"]},
        },
        "required": ["category", "cuisines", "features", "name_words", "area"],
    }


def clean_intent(raw: SearchIntent, city: str, cuisines: list[str]) -> SearchIntent:
    """enforce what the schema can't: real cuisines, useful name words, a real area."""
    allowed = set(cuisines)
    city_lower = city.strip().lower()

    area = (raw.area or "").strip() or None

    # area words in name_words match whatever is named after the area.
    place_words = set(f"{city_lower} {(area or '').lower()}".split())

    words: list[str] = []
    for word in raw.name_words:
        cleaned = word.strip().lower()
        if (
            3 <= len(cleaned) <= 30
            and cleaned not in GENERIC_NAME_WORDS
            and cleaned not in place_words
            and cleaned not in words
        ):
            words.append(cleaned)

    # naming the city itself isn't an area within it.
    if area is not None and area.lower() in {city_lower, f"{city_lower} city"}:
        area = None

    return SearchIntent(
        category=raw.category,
        # dedupe, keeping order.
        cuisines=list(dict.fromkeys(c for c in raw.cuisines if c in allowed)),
        features=list(dict.fromkeys(raw.features)),
        name_words=words[:MAX_NAME_WORDS],
        area=area,
    )


class OllamaClient:
    """a local ollama server. one http call per question."""

    def parse(
        self,
        question: str,
        city: str,
        cuisines: list[str],
        previous: SearchIntent | None = None,
    ) -> SearchIntent:
        system = (
            SYSTEM_PROMPT.format(city=city)
            + "\n\nAllowed cuisines: "
            + (", ".join(cuisines) if cuisines else "(none)")
        )
        if previous is not None:
            system += FOLLOW_UP_PROMPT.format(
                previous=previous.model_dump_json()
            )

        body = {
            "model": settings.OLLAMA_MODEL,
            "stream": False,
            # skip qwen3's thinking pass; it doubles the wait for no gain here.
            "think": False,
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            # deterministic: the same question gives the same search.
            "options": {"temperature": 0},
            "format": _schema(cuisines),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        }

        try:
            response = httpx.post(
                f"{settings.OLLAMA_URL}/api/chat",
                json=body,
                timeout=settings.OLLAMA_TIMEOUT_SECONDS,
            )
        except httpx.ConnectError as exc:
            raise LlmUnavailable(
                "Pina's assistant isn't running. Start it with: "
                "brew services start ollama"
            ) from exc
        except httpx.TimeoutException as exc:
            raise LlmUnavailable(
                "The assistant took too long to answer. Try again — the first "
                "question after a break also has to load the model."
            ) from exc
        except httpx.HTTPError as exc:
            raise LlmUnavailable(f"Could not reach the assistant: {exc}") from exc

        if response.status_code == 404:
            raise LlmUnavailable(
                f"The model {settings.OLLAMA_MODEL} isn't downloaded. "
                f"Run: ollama pull {settings.OLLAMA_MODEL}"
            )
        if response.is_error:
            raise LlmError(f"The assistant failed (HTTP {response.status_code}).")

        try:
            content = response.json()["message"]["content"]
            raw = SearchIntent.model_validate(json.loads(content))
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            # ValueError covers bad json and bad enum values.
            logger.warning("unusable LLM answer: %r", response.text[:500])
            raise LlmError(
                "The assistant gave an answer Pina couldn't read. "
                "Try rephrasing the question."
            ) from exc

        logger.info("ask %r -> %s", question, raw.model_dump_json())
        return clean_intent(raw, city, cuisines)


_client = OllamaClient()


def get_llm() -> Llm:
    """fastapi dependency. tests swap this for a fake."""
    return _client
