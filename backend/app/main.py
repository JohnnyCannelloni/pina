"""The FastAPI application object — what uvicorn runs.

Deliberately almost empty. Its whole job is to create the app and attach the
router, so that "where do the endpoints live?" has one answer: routes.py.

Start it with:
    uvicorn app.main:app --reload
             ^^^^^^^^^ ^^^
             module    the `app` variable below
"""

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
    """Startup and shutdown. Currently one job: crash recovery.

    `sync_state = 'syncing'` means "a background job is working on this right
    now". If the process dies mid-sync — Ctrl-C, a reload, a crash — that claim
    becomes a lie that nothing will ever correct, and the UI shows a progress
    spinner forever for work that stopped hours ago.

    Nothing can be syncing at the moment we start up, by definition, so any
    destination still marked `syncing` is a leftover. Resetting it to `pending`
    makes it resumable: the tiles that succeeded are still DONE, so a retry
    picks up only what is left.

    This is a general lesson about storing in-progress state in a database —
    "in progress" is a claim about a process, and processes die. Something has
    to reconcile the row with reality on the way back up.
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

# CORS is redundant in the normal setup: Vite proxies /api to this server (see
# frontend/vite.config.ts), so the browser sees one origin and never sends a
# cross-origin request. It is here anyway because the day you open the React app
# on some other port, the failure is a silent console error that looks nothing
# like a CORS problem. Four lines to not lose an hour to that.
#
# Localhost origins only — this app is explicitly single-user and local.
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
    """Liveness check. Useful for confirming the API is up without a database."""
    return {"status": "ok"}
