"""The FastAPI application object — what uvicorn runs.

Deliberately almost empty. Its whole job is to create the app and attach the
router, so that "where do the endpoints live?" has one answer: routes.py.

Start it with:
    uvicorn app.main:app --reload
             ^^^^^^^^^ ^^^
             module    the `app` variable below
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import router

app = FastAPI(
    title="Pina",
    description="A single-user travel planner.",
    version="0.1.0",
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
