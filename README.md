# Pina

A single-user travel planner. A **trip** has **destinations**; each destination
collects **places** — restaurants, cafés, and sights — which can be flagged
*Must-go* and optionally pinned on a map.

Currently seeded with one trip (Italy) and one destination (Venice).

Stack: FastAPI · PostgreSQL · SQLAlchemy · Alembic · React · TypeScript · Vite ·
Leaflet.

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

## How a save works

Worth tracing once, in this order:

1. **`backend/alembic/versions/0001_initial.py`** — the three tables, and the
   constraints that keep the data honest.
2. **`backend/app/routes.py`** — the three endpoints;
   `create_place` is the one the Save button hits. From there:
   `schemas.py` (validation) → `models.py` (the ORM objects) → `db.py` (the session).
3. **`frontend/src/AddPlaceForm.tsx`** — `handleSubmit`, where a save begins.
4. **`frontend/src/LocationPicker.tsx`** — where the optional pin's coordinates
   come from.

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
