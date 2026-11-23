"""the fastapi app. run with `uvicorn app.main:app --reload`."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import update

from app.db import SessionLocal
from app.models import Destination, SyncState
from app.routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """on startup, reset destinations left in `syncing` by a previous run.

    nothing can be syncing before the app has started, so those rows are
    leftovers from a crash or reload. moving them back to `pending` makes them
    retryable instead of spinning forever in the ui.
    """
    with SessionLocal() as db:
        recovered = db.execute(
            update(Destination)
            .where(Destination.sync_state == SyncState.SYNCING)
            .values(
                sync_state=SyncState.PENDING,
                sync_error="The previous sync was interrupted. Retry to resume.",
            )
        ).rowcount
        db.commit()

    if recovered:
        logger.warning(
            "reset %s destination(s) left mid-sync by a previous run", recovered
        )

    yield


app = FastAPI(
    title="Pina",
    description="A single-user travel planner.",
    version="0.1.0",
    lifespan=lifespan,
)

# vite proxies /api in dev, so this only matters if the frontend runs elsewhere.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """liveness check that doesn't touch the database."""
    return {"status": "ok"}
