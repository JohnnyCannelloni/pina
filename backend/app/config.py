"""Application settings, read once from the environment.

Why a settings object instead of os.environ.get() scattered around: the database
URL is needed by three different entry points (the API, the seed script, and
Alembic). Having one place that reads it means there is one answer to "which
database am I talking to?", and a missing value fails loudly at import time
rather than halfway through a request.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # An absolute path, not just ".env" — otherwise the file is only found
        # when you happen to run commands from inside backend/.
        env_file=BACKEND_DIR / ".env",
        extra="ignore",
    )

    # No default. If DATABASE_URL is missing, pydantic raises on startup with a
    # message naming the field, which is far easier to debug than a connection
    # error against some implicit fallback.
    DATABASE_URL: str

    # Only pytest uses this; see tests/conftest.py.
    TEST_DATABASE_URL: str = (
        "postgresql+psycopg://pina:pina@localhost:5432/pina_test"
    )

    # --- place data (Geofabrik OpenStreetMap extracts) ---
    # These have defaults because, unlike the database, the app runs fine
    # without them being set — and nothing here is a secret.

    #: Identifies us to Geofabrik and Nominatim. Both ask clients to say who
    #: they are, and anonymous traffic from a default library agent is what gets
    #: an IP throttled.
    USER_AGENT: str = "Pina/0.1 (single-user travel planner; local development)"

    #: Where downloaded .osm.pbf files are kept. On disk, not in Postgres:
    #: they are large, opaque and re-downloadable, so `rm -rf` is a valid cache
    #: reset. Outside the repo so a stray `git add .` cannot commit 600 MB.
    EXTRACT_CACHE_DIR: str = "~/.cache/pina/extracts"

    #: Whether adding a destination may start a region download by itself.
    #:
    #: True (the default) is right for local single-user use: you add a city and
    #: it fills itself. Set it False for a hosted deployment, and a client can
    #: no longer trigger a several-hundred-megabyte download by typing a city
    #: name — the destination reports that its region is not loaded, and an
    #: operator runs `python -m app.fill_places --region <id>`.
    #:
    #: This is the smallest honest expression of "clients use my servers and
    #: download nothing". Real multi-tenancy (accounts owning saved places) is
    #: still future work, because login is.
    AUTO_IMPORT_REGIONS: bool = True

    #: Read timeout per chunk while streaming a download — not for the whole
    #: transfer, which legitimately takes minutes for a large country. This
    #: catches a stalled connection without aborting a healthy slow one.
    EXTRACT_READ_TIMEOUT_SECONDS: float = 120.0

    # --- geocoding (turning "Lisbon" into a destination) ---
    NOMINATIM_URL: str = "https://nominatim.openstreetmap.org/search"
    # Measured at 0.45s, so 15s is generous. Short enough that a hung lookup
    # does not leave someone staring at a spinner.
    NOMINATIM_TIMEOUT_SECONDS: float = 15.0


settings = Settings()  # type: ignore[call-arg]  # values come from .env
