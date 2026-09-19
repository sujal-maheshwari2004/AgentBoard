"""Stage 0 stub. T7 replaces this with the real server (see docs/CONTRACTS.md §10)."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI


def create_app(root: Path) -> FastAPI:
    app = FastAPI(title="whiteboard (stub)")

    @app.get("/api/health")
    def health() -> dict:
        return {"project": str(root.resolve()), "pid": os.getpid(), "stub": True}

    return app


def run_foreground(root: Path) -> None:  # pragma: no cover - replaced by T7
    import uvicorn

    uvicorn.run(create_app(root), host="127.0.0.1", port=int(os.environ.get("WHITEBOARD_PORT", "43000")), log_config=None)
