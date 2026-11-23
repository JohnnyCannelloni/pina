# Pina

A travel planner for collecting the places you want to go. A trip has
destinations, and each destination has places (restaurants, cafés and sights)
that you can mark as must-go and pin on a map.

Add a city and Pina fills its map with places from OpenStreetMap. Then ask for
what you want in plain words, like *"italian with outdoor seating near Alfama"*,
and follow up with *"only vegan ones"* or *"what about Baixa?"*. Everything runs
locally and needs no API keys.

**Stack:** FastAPI, PostgreSQL, SQLAlchemy, Alembic, React, TypeScript, Vite,
Leaflet, OpenStreetMap (Nominatim and Geofabrik), and Ollama for the local LLM.

## Features

- **Destinations anywhere.** Search for a city, pick the right match, and its
  restaurants, cafés and sights load in the background.
- **A live map.** Places load as you pan. Zoomed out, they're grouped into
  numbered bubbles. Filter by category, and save a place in two taps.
- **Ask Pina.** A chat that turns your request into a search. Results are always
  real places from the map data; the model never makes places up.
- **Place details.** Cuisine, opening hours, website, phone and features like
  outdoor seating or vegan options, wherever OpenStreetMap has them.
- **Photos.** Every place links to its Google Maps page for photos and reviews.
- **Your list.** Saved places with must-go flags, in a list or on the map.
  Places and destinations can be deleted.

## Getting started

You'll need Docker, Python 3.11+, Node 20+ and, for Ask Pina,
[Ollama](https://ollama.com).

**1. Start Postgres** from the repo root:

```bash
docker compose up -d --wait
```

This creates two databases: `pina` for the app and `pina_test` for the tests.

**2. Set up the backend:**

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
python -m app.seed        # creates the trip (italy, with venice as a first destination)
uvicorn app.main:app --reload --port 8000
```

API docs are at <http://localhost:8000/docs>.

**3. Set up the local LLM** (only needed for Ask Pina):

```bash
brew install ollama
brew services start ollama
ollama pull qwen3:8b      # about 5 GB
```

To use a different model, set `OLLAMA_MODEL` in `backend/.env`. On an M1,
`qwen3:8b` answers in about 5 seconds; `qwen3:4b` is faster but a bit less
accurate.

**4. Start the frontend** in a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. Vite forwards `/api` to the backend, so both need
to be running.

## Map data

Places come from [Geofabrik](https://download.geofabrik.de)'s OpenStreetMap
extracts. The first city you add in a region downloads that region's file once,
and later cities in the same region load in seconds. Pina picks the smallest
region that contains the city:

| City | Downloads | Instead of |
| --- | --- | --- |
| New York City | `us/new-york`, 476 MB | `us`, 11.7 GB |
| Venice | `nord-est`, 598 MB | `italy`, 2.1 GB |

Downloads resume if they're interrupted. Files are cached in
`~/.cache/pina/extracts`, and deleting that folder is safe.

You can also manage this from the command line:

```bash
python -m app.fill_places --list                     # destinations and place counts
python -m app.fill_places --region portugal          # download and import a region
python -m app.fill_places --list-regions --near "New York City"
```

Re-running `--region` on a region you already have re-imports it from the cached
file, which is how you pick up new tags after a migration.

To run Pina for other people, set `AUTO_IMPORT_REGIONS=false` so that adding a
city can't start a large download, and import regions with `--region` instead.

## How Ask Pina works

1. The local model reads your message and fills in a structured search:
   category, cuisines, features, name hints and area. Ollama constrains its
   output to a JSON schema, and the cuisines it can pick are the ones that
   actually exist in your destination.
2. A named area like "Alfama" is looked up inside the destination.
3. The search runs against the cached places in Postgres, closest first.
4. Pina's reply is written from the results, so it only says what the data
   shows. Follow-up messages send the previous search along to be refined.

Only [backend/app/llm.py](backend/app/llm.py) talks to the model, so moving to a
hosted model later means changing that one file.

## Tests

```bash
cd backend
source .venv/bin/activate
pytest
```

The tests run against `pina_test` with the real migrations, and each one is
rolled back afterwards. The geocoder, the LLM and the downloads are faked, so
the suite needs no network or Ollama and never touches your own data.

For the frontend:

```bash
cd frontend
npm run typecheck
npm run build
```

## Handy commands

| Command | What it does |
| --- | --- |
| `alembic current` | Show which migration the database is on |
| `alembic downgrade base` | Drop all of Pina's tables |
| `docker compose down` | Stop Postgres and keep the data |
| `docker compose down -v` | Stop Postgres and delete the data |
| `docker exec -it pina-db psql -U pina -d pina` | Open a SQL prompt |

## Project layout

```
backend/
  app/
    routes.py      http endpoints
    models.py      database tables
    schemas.py     request and response shapes
    ask.py         ask pina's search and replies
    llm.py         the ollama client
    extract.py     downloading and importing osm extracts
    sync.py        background syncing per destination
    regions.py     picking the right geofabrik region
    geocode.py     nominatim lookups
  alembic/         migrations
  tests/
frontend/
  src/
    AskPina.tsx    the chat
    MapView.tsx    the map
    ...
```

## Not built yet

- Review summaries from the web, through a self-hosted SearXNG
- Editing a saved place, or moving its pin
- Accounts and sharing
- Itineraries
