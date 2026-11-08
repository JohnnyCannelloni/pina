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


settings = Settings()  # type: ignore[call-arg]  # values come from .env
