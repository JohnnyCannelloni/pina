# Pina

A single-user travel planner. A **trip** has **destinations**; each destination
collects **places** — restaurants, cafés, and sights — which can be flagged
*Must-go* and optionally pinned on a map.

**Add any city and the map fills itself in.** Type "Lisbon", pick it from the
matches, and Pina fetches its restaurants, cafés, and sights from OpenStreetMap
in the background. They appear on the map as you pan — no button to press — and
tapping one saves it to your collection in two taps. Filter chips narrow the map
by category, and clicking a row in the Places list flies to that pin.

Seeded with one trip (Italy) and one destination (Venice), but Venice is only an
example: it goes through the same code path as every city you add.

Stack: FastAPI · PostgreSQL · SQLAlchemy · Alembic · React · TypeScript · Vite ·
Leaflet · OpenStreetMap (Nominatim + Geofabrik).

No API keys are needed for any of it.

---

## Running it locally

You need Docker Desktop, Python 3.11+, and Node 20+.

### 1. Start Postgres

From the repository root:

```bash
docker compose up -d --wait
```

This runs `postgres:17` on port 5432 and creates two databases: `pina` for the
app and `pina_test` for the test suite. `--wait` blocks until Postgres is
actually accepting connections, which stops the next step from racing it.

### 2. Set up the backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # the defaults already match docker-compose.yml
```

### 3. Create the tables and seed the trip

```bash
alembic upgrade head          # creates trips, destinations, places
python -m app.seed            # inserts Italy -> Venice
```

`app.seed` is idempotent — running it twice does not create a second Venice.

### 4. Run the API

```bash
uvicorn app.main:app --reload --port 8000
```

- Interactive API docs: <http://localhost:8000/docs>
- Health check: <http://localhost:8000/health>

### 5. Run the UI

In a **second terminal**:

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>.

Vite proxies `/api` to port 8000, so both servers must be running — but the
browser only ever talks to 5173, which is why there is no CORS setup to get
wrong.

---

## Tests

```bash
cd backend
source .venv/bin/activate
pytest
```

These are integration tests: they run against a real Postgres (`pina_test`),
with the schema built by the real Alembic migration, through a real FastAPI
client. They never touch the `pina` database, so your own saved places are safe.

## Other useful commands

| Command | What it does |
| --- | --- |
| `alembic current` | Which migration the database is on |
| `alembic history` | All migrations, in order |
| `alembic downgrade base` | Drop everything this project created |
| `docker compose down` | Stop Postgres, keep the data |
| `docker compose down -v` | Stop Postgres and delete the data (then re-run step 3) |
| `docker exec -it pina-db psql -U pina -d pina` | A SQL prompt on your data |
| `cd frontend && npm run typecheck` | TypeScript, no build |
| `cd frontend && npm run build` | Production build |

---

### 6. Fill in a destination's places

The map draws its markers from a local cache, filled from an OpenStreetMap
extract. Adding a destination through the UI starts that automatically. To fill
the seeded Venice, or to watch a big download rather than leave it to a
background task:

```bash
python -m app.fill_places --list                    # what is cached so far
python -m app.fill_places --destination 1           # fill one
python -m app.fill_places --region us/new-york      # fill a region directly
python -m app.fill_places --list-regions --near "New York City"
```

**The first destination in a region downloads that region's map data**, once.
Every later city in the same region is filled from the cached file in seconds.
Downloads resume if interrupted, and re-running is always safe.

Pina picks the **smallest** published region containing your city, which matters
a great deal:

| city | downloads | instead of |
| --- | --- | --- |
| New York City | `us/new-york` — 476 MB | `us` — 11,706 MB |
| Venice | `nord-est` — 598 MB | `italy` — 2,149 MB |
| Tokyo | `kanto` | `japan` — 1,600 MB |

Use `--list-regions --near` to see what it chose *and what it chose over*.

---

## How the map streams

The browser never holds a city's worth of markers. Every pan asks the server for
just the rectangle on screen, and the server decides what is useful to send:

- **Few enough to draw** → real pins you can tap and save.
- **Too many** → counts per grid cell, aggregated in SQL. Lisbon's city view is
  4,383 places returned as 66 numbered bubbles, in ~35 ms. Tap one to zoom in.

The decision is made from the **actual count**, not from the zoom level, because
density varies far more than zoom does — a suburb at zoom 12 should still show
pins, and midtown Manhattan at zoom 15 should not show ten thousand.

The response also says whether the area is **covered**: an empty map now
explains whether there is genuinely nothing there or the region was never
imported. Those used to look identical.

Clients download nothing. If you deploy this for real, set
`AUTO_IMPORT_REGIONS=false` and a visitor typing a city name can no longer start
a 476 MB download — an operator runs `--region` instead.

## Where the place data comes from

Two different OpenStreetMap services, for two different jobs:

| job | service | why |
| --- | --- | --- |
| "where is Lisbon?" | **Nominatim** | one small lookup per destination you add; measured at 0.45 s |
| "every restaurant in Portugal" | **Geofabrik** static extracts | bulk data, no API, no rate limit, no key |

Pina originally used the **Overpass API** for the second job. It is the right
*kind* of service, but its public instances are busy by default — measured over
an afternoon it returned `502 all query workers are busy`, a 25-second timeout,
or `429`, on the large majority of requests. A feature built on it did not work.

Static files have no query workers to be busy. A country downloads once; parsing
it is fast — **Malta's 8.6 MB yields 1,749 named places in under a second**.

Consequences worth knowing:

- The map **never** makes an outbound call. It reads an indexed Postgres table,
  so it answers in milliseconds and works offline.
- **Your saved places are never touched** by a refill. `places` is your
  collection; `osm_places` is a disposable cache.
- Extracts are cached in `~/.cache/pina/extracts` (set `EXTRACT_CACHE_DIR` to
  change it). Deleting that directory is a safe cache reset.
- Regions come from Geofabrik's own published index of **554 regions** (3.6 MB,
  cached), not a hand-maintained table — so there is nothing to drift, and
  sub-regions like `us/new-york` are reachable. Somewhere no region covers still
  gives you a working destination; it just says so instead of silently showing
  an empty map.

## How a save works

Worth tracing once, in this order:

1. **`backend/alembic/versions/0001_initial.py`** — the three tables, and the
   constraints that keep the data honest.
2. **`backend/app/routes.py`** — the four endpoints;
   `create_place` is the one the Save button hits. From there:
   `schemas.py` (validation) → `models.py` (the ORM objects) → `db.py` (the session).
3. **`frontend/src/AddPlaceForm.tsx`** — `handleSubmit`, where a save begins.
4. **`frontend/src/LocationPicker.tsx`** — where the optional pin's coordinates
   come from.

## How place discovery works

Reading order:

1. **`backend/alembic/versions/0002_add_place_source.py`** — an additive
   migration against a table that already has rows, and why `server_default` is
   mandatory there.
2. **`backend/alembic/versions/0003_destination_areas_and_osm_cache.py`** — the
   three-step `nullable → backfill → NOT NULL` pattern, for a required column
   with no sensible constant default.
3. **`backend/alembic/versions/0004_osm_extracts.py`** — swapping out a data
   source, and deleting the table that existed only to cope with the old one's
   failures.
4. **`backend/app/extract.py`** — downloading and parsing someone else's 600 MB
   file: resumable streaming, a `.part` suffix as the "is this usable" marker,
   a filter pushed into C++ so most of a country never reaches Python, and a
   batched upsert.
5. **`backend/app/sync.py`** — background work: why it opens its own session,
   and why one pass fills every city in a country.
6. **`backend/app/geocode.py`** — the other third-party client, and why each
   service is right for only one of the two jobs.
7. **`backend/app/routes.py`** — `nearby` as a plain indexed `SELECT`, and the
   `IntegrityError` → `409 Conflict` translations.
8. **`backend/tests/conftest.py`** and **`test_extract.py`** — how dependency
   overrides keep the suite off the network, and how writing a real 216-byte
   `.osm.pbf` beats mocking a parser.

### The three tables, and why they are separate

| table | what it is | if you deleted it |
| --- | --- | --- |
| `places` | **your** collection — curated, has `must_go` | you lose your trip |
| `osm_places` | a **global cache** of what exists in the world | refill it, lose nothing |
| `osm_extracts` | which region files have been imported, and where they cover | re-download them |

`osm_places` is global: one row per OpenStreetMap place, shared by every
destination and every client. Importing a region once serves everyone. Your
*collection* stays per-destination, so saving a café in Venice does not mark it
saved in Mestre.

Keeping the cache separate from the collection is what makes refilling a city
safe. If the map read from one shared table, refreshing Lisbon could delete a
restaurant you had saved — and it is why swapping Overpass for Geofabrik
changed neither the table the map reads nor anything you had saved.

The request path, end to end:

```
AddPlaceForm.handleSubmit
  -> api.ts createPlace()
  -> POST /api/destinations/1/places     (Vite proxies 5173 -> 8000)
  -> routes.py create_place()
  -> schemas.py PlaceCreate validates    (422 if the body is wrong)
  -> models.py Place(...)
  -> db.commit()                         -> INSERT INTO places
  -> schemas.py PlaceRead serialises the saved row
  -> DestinationPage prepends it to state -> list and map both update
```

Field names are identical at every layer — `must_go` is `must_go` in the form,
the JSON, the Python model, and the Postgres column — so you can grep one name
across the whole stack.

## Not built yet

LLM place recommendations, itinerary generation, login, and social features.
Also: editing or deleting a saved place, and moving a pin after saving.
