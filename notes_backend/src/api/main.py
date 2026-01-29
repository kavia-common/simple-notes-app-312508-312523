from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict, field_validator

openapi_tags = [
    {"name": "Health", "description": "Service health and basic diagnostics."},
    {"name": "Notes", "description": "CRUD operations for notes stored in SQLite."},
]


def _utc_now_iso() -> str:
    """Return current UTC time as an ISO8601 string with timezone."""
    return datetime.now(timezone.utc).isoformat()


# PUBLIC_INTERFACE
def get_db_path() -> str:
    """Resolve the SQLite database path.

    Resolution order:
    1) SQLITE_DB environment variable (provided by database container conventions)
    2) Fallback to the known workspace default path used by the database container

    Returns:
        Absolute path to the SQLite database file.
    """
    env_path = os.getenv("SQLITE_DB")
    if env_path:
        return env_path

    # Default according to database/db_connection.txt in the database container.
    return "/home/kavia/workspace/code-generation/simple-notes-app-312508-312525/database/myapp.db"


@contextmanager
def _db_conn() -> sqlite3.Connection:
    """Context manager yielding a SQLite connection with row factory."""
    db_path = get_db_path()
    # Ensure directory exists (safe for container environments).
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _ensure_schema() -> None:
    """Create notes table if it doesn't exist.

    Database container likely already does this, but keeping it here makes backend robust
    (e.g., local runs, fresh volumes).
    """
    with _db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              content TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


class Note(BaseModel):
    """A persisted note returned to clients."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Unique identifier of the note.")
    title: str = Field(..., description="Short title of the note.")
    content: str = Field(..., description="Body content of the note.")
    created_at: str = Field(..., description="UTC ISO8601 timestamp when the note was created.")
    updated_at: str = Field(..., description="UTC ISO8601 timestamp when the note was last updated.")


class NoteCreate(BaseModel):
    """Payload to create a note."""

    title: str = Field(..., min_length=1, max_length=200, description="Non-empty title (max 200 chars).")
    content: str = Field(
        default="",
        max_length=10_000,
        description="Note content (max 10,000 chars). Can be empty.",
    )

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, v: str) -> str:
        v2 = v.strip()
        if not v2:
            raise ValueError("title must not be empty")
        return v2


class NoteUpdate(BaseModel):
    """Payload to update a note (full update)."""

    title: str = Field(..., min_length=1, max_length=200, description="Non-empty title (max 200 chars).")
    content: str = Field(
        default="",
        max_length=10_000,
        description="Note content (max 10,000 chars). Can be empty.",
    )

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, v: str) -> str:
        v2 = v.strip()
        if not v2:
            raise ValueError("title must not be empty")
        return v2


def _row_to_note(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert sqlite row to API note shape."""
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "content": str(row["content"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


app = FastAPI(
    title="Simple Notes API",
    description="FastAPI backend for a simple notes app (SQLite datastore).",
    version="1.0.0",
    openapi_tags=openapi_tags,
)

# Keep permissive CORS for frontend preview environments.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup() -> None:
    """Initialize database schema on startup."""
    _ensure_schema()


@app.get("/", tags=["Health"], summary="Health check", description="Basic service health check.")
def health_check() -> Dict[str, str]:
    """Return a basic health response."""
    return {"message": "Healthy"}


@app.get(
    "/notes",
    response_model=List[Note],
    tags=["Notes"],
    summary="List notes",
    description="Return all notes ordered by most recently updated first.",
)
def list_notes() -> List[Dict[str, Any]]:
    """List all notes."""
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    return [_row_to_note(r) for r in rows]


@app.post(
    "/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    tags=["Notes"],
    summary="Create note",
    description="Create a new note.",
)
def create_note(payload: NoteCreate) -> Dict[str, Any]:
    """Create a note and return it."""
    now = _utc_now_iso()
    with _db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO notes (title, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (payload.title, payload.content, now, now),
        )
        note_id = int(cur.lastrowid)
        conn.commit()
        row = conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()

    # row should exist immediately after insert; still guard just in case.
    if row is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create note")
    return _row_to_note(row)


@app.get(
    "/notes/{id}",
    response_model=Note,
    tags=["Notes"],
    summary="Get note",
    description="Get a single note by id.",
)
def get_note(id: int = Field(..., ge=1, description="Note id (positive integer).")) -> Dict[str, Any]:
    """Fetch a single note."""
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return _row_to_note(row)


@app.put(
    "/notes/{id}",
    response_model=Note,
    tags=["Notes"],
    summary="Update note",
    description="Replace a note's title/content.",
)
def update_note(
    payload: NoteUpdate,
    id: int = Field(..., ge=1, description="Note id (positive integer)."),
) -> Dict[str, Any]:
    """Update an existing note (full update)."""
    now = _utc_now_iso()
    with _db_conn() as conn:
        # Check existence first for correct 404 semantics.
        exists = conn.execute("SELECT 1 FROM notes WHERE id = ?", (id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")

        conn.execute(
            "UPDATE notes SET title = ?, content = ?, updated_at = ? WHERE id = ?",
            (payload.title, payload.content, now, id),
        )
        conn.commit()

        row = conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (id,),
        ).fetchone()

    if row is None:
        # Extremely unlikely: record disappeared between update and fetch.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return _row_to_note(row)


@app.delete(
    "/notes/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Notes"],
    summary="Delete note",
    description="Delete a note by id.",
)
def delete_note(
    id: int = Field(..., ge=1, description="Note id (positive integer)."),
) -> Response:
    """Delete a note. Returns 204 if deleted, 404 if not found."""
    with _db_conn() as conn:
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (id,))
        conn.commit()

    if cur.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")

    return Response(status_code=status.HTTP_204_NO_CONTENT)
