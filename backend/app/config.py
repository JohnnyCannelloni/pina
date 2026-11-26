"""settings, read once from the environment and backend/.env."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # absolute, so it's found no matter which directory you run from.
        env_file=BACKEND_DIR / ".env",
        extra="ignore",
    )

    # required: fail at startup rather than on the first query.
    DATABASE_URL: str

    # used by tests/conftest.py only.
    TEST_DATABASE_URL: str = (
        "postgresql+psycopg://pina:pina@localhost:5432/pina_test"
    )

    # --- place data (overpass api) ---

    #: overpass and nominatim both ask clients to identify themselves.
    USER_AGENT: str = "Pina/0.1 (single-user travel planner; local development)"

    #: any overpass api instance. the public one asks for fair use: a query
    #: per destination is fine, bulk scraping isn't.
    OVERPASS_URL: str = "https://overpass-api.de/api/interpreter"
    #: server-side query timeout; the http timeout is a little longer.
    OVERPASS_TIMEOUT_SECONDS: int = 90

    # --- geocoding ---
    NOMINATIM_URL: str = "https://nominatim.openstreetmap.org/search"
    NOMINATIM_TIMEOUT_SECONDS: float = 15.0

    # --- ask pina (local llm through ollama, see app/llm.py) ---
    OLLAMA_URL: str = "http://localhost:11434"
    #: any pulled model. qwen3:8b takes ~5 s per question on an m1.
    OLLAMA_MODEL: str = "qwen3:8b"
    #: includes loading the model on the first question after a break.
    OLLAMA_TIMEOUT_SECONDS: float = 90.0
    #: how long ollama keeps the model in memory between questions.
    OLLAMA_KEEP_ALIVE: str = "30m"


settings = Settings()  # type: ignore[call-arg]  # values come from .env
