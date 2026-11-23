"""The local LLM: turning a question into a search, through Ollama.

READ THIS FIRST for Ask Pina, then app/ask.py.

**The one rule: the LLM never names a place.** A small local model asked to
"recommend Italian restaurants in Lisbon" invents names and addresses with
total confidence. So it is given a narrower job that small models do well:
read a sentence and fill in a form. The form is `SearchIntent`; app/ask.py
then runs it against osm_places, so every result is a real, mapped place.

**Structured outputs, not tool calling.** Ollama's `format` field takes a JSON
schema and *constrains decoding* to it: the model cannot emit a token that
would break the schema. That is stronger than asking nicely for JSON, and
more reliable on an 8B model than tool calling. Two consequences worth seeing:

  - `features` is an enum, so the model can only pick things the data can
    answer.
  - `cuisines` is an enum of the cuisine values that *actually exist in this
    destination*, built per request. "a pasta place" can only become
    "italian", never a made-up tag that matches nothing.

This is the only file that knows Ollama exists. Swapping in a hosted model
later means replacing `OllamaClient` and nothing else.
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

#: Words that describe *every* place of a category, so searching names for them
#: would only throw away good matches ("cafe with wifi" must not require
#: "cafe" in the name). Dropped from name_words whatever the model says.
GENERIC_NAME_WORDS = frozenset(
    {
        "restaurant", "restaurante", "ristorante", "cafe", "café", "caffè",
        "coffee", "bar", "food", "place", "places", "eat", "eatery", "shop",
    }
)

MAX_NAME_WORDS = 5


class LlmError(RuntimeError):
    """The model answered, but not with something usable."""


class LlmUnavailable(LlmError):
    """Ollama is not running, or the model is not pulled.

    A separate type so the route can answer 503 with the command that fixes
    it, rather than a generic failure.
    """


class Llm(Protocol):
    """What app/ask.py needs from a model. Tests supply a fake."""

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

#: Added when the message follows an earlier one. Small models need the
#: examples: without "keep everything else", "only vegan ones" tends to come
#: back as a search for vegan anything, anywhere.
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
    """The JSON schema Ollama must follow, with this destination's cuisines.

    Written by hand rather than SearchIntent.model_json_schema(): the cuisine
    enum is per-request, and a flat schema with no $refs is what small models
    follow most reliably.
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
                # No cuisine data here means nothing to choose from.
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
    """Tidy what the model said, without trusting it.

    The schema already constrains the shape; this enforces the meaning. It is
    a separate function so the rules are tested directly, without a model.
    """
    allowed = set(cuisines)
    city_lower = city.strip().lower()

    area = (raw.area or "").strip() or None

    # Words from the place names themselves ("museums in Belém" -> "belém")
    # would match whatever happens to be named after the area — measured, a
    # pastry shop ranked among the museums.
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

    # "pizza in Lisbon" names the city, not an area inside it. Searching for
    # "Lisbon" inside Lisbon would just give us the city centre point.
    if area is not None and area.lower() in {city_lower, f"{city_lower} city"}:
        area = None

    return SearchIntent(
        category=raw.category,
        # dict.fromkeys de-duplicates while keeping the model's order.
        cuisines=list(dict.fromkeys(c for c in raw.cuisines if c in allowed)),
        features=list(dict.fromkeys(raw.features)),
        name_words=words[:MAX_NAME_WORDS],
        area=area,
    )


class OllamaClient:
    """Talks to a local Ollama server. One HTTP call per question."""

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
            # qwen3 "thinks" out loud before answering by default, which
            # doubles the wait for a task this simple.
            "think": False,
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            # 0: the same question should give the same search.
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
            # ValueError covers both bad JSON and a bad enum value.
            logger.warning("unusable LLM answer: %r", response.text[:500])
            raise LlmError(
                "The assistant gave an answer Pina couldn't read. "
                "Try rephrasing the question."
            ) from exc

        logger.info("ask %r -> %s", question, raw.model_dump_json())
        return clean_intent(raw, city, cuisines)


_client = OllamaClient()


def get_llm() -> Llm:
    """FastAPI dependency. The same testing seam as get_geocoder."""
    return _client
