"""SQLite: sesiones y un log de eventos de solo agregado.

El visor no comparte memoria con el servidor MCP: lee los eventos nuevos de la base. Así funciona igual si el
visor corre en el mismo proceso, en otro (`casebook viewer`) o si hay varias sesiones de Claude.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    card        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    query_id    TEXT,
    type        TEXT NOT NULL,
    ts          TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
CREATE TABLE IF NOT EXISTS viewer_health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        from .review import Review, SCHEMA as REVIEW_SCHEMA
        self._conn.executescript(REVIEW_SCHEMA)
        self.review = Review(self)
        # Nombres provisionales de versiones anteriores; conserva IDs e historial.
        self._conn.execute(
            "UPDATE sessions SET name = COALESCE(NULLIF(card, ''), 'Sin identificar · ' || id) "
            "WHERE name GLOB 'sin-nombre-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
        )

    @property
    def knowledge(self):
        # Compartido por visor y recorder; no abre knowledge.db hasta que se usa.
        if not hasattr(self, "_knowledge"):
            from .knowledge import Knowledge
            self._knowledge = Knowledge(self)
        return self._knowledge

    def close(self) -> None:
        self._conn.close()

    def save_viewer_health(self, health: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO viewer_health (id, payload) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (json.dumps(health, ensure_ascii=False),),
            )

    def viewer_health(self) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT payload FROM viewer_health WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None

    # --- sesiones -------------------------------------------------------------------------------------------

    def create_session(self, name: str | None, card: str | None = None) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (name, card, created_at) VALUES (?, ?, ?)", (name or card or "Sin identificar", card, now())
            )
            if not name and not card:
                self._conn.execute("UPDATE sessions SET name = ? WHERE id = ?",
                                   (f"Sin identificar · {cur.lastrowid}", cur.lastrowid))
        return self.get_session(cur.lastrowid)

    def update_session(self, session_id: int, name: str | None = None, card: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                previous = self.get_session(session_id)
                if previous is None:
                    raise ValueError(f"no existe la sesión {session_id}")
                updated_name = name if name is not None else (card or previous["name"])
                updated_card = card if card is not None else previous["card"]
                self._conn.execute("UPDATE sessions SET name = ?, card = ? WHERE id = ?",
                                   (updated_name, updated_card, session_id))
                if (updated_name, updated_card) != (previous["name"], previous["card"]):
                    payload = {"name": updated_name, "card": updated_card,
                               "previous": {"name": previous["name"], "card": previous["card"]}}
                    self._conn.execute(
                        "INSERT INTO events (session_id, type, ts, payload) VALUES (?, ?, ?, ?)",
                        (session_id, "session.updated", now(), json.dumps(payload, ensure_ascii=False)))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_session(session_id)

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return {**dict(row), "loop": self.review.get(session_id)} if row else None

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT s.*, COALESCE(SUM(e.type != 'slack.progress'), 0) AS events, MAX(e.ts) AS last_event_at,
                      MAX(e.id) AS last_event_id,
                      COALESCE(SUM(e.type IN ('query.failed', 'query.rejected')), 0) AS errors,
                      COALESCE(MAX(e.type = 'slack'), 0) AS slack
               FROM sessions s LEFT JOIN events e ON e.session_id = s.id
               GROUP BY s.id
               ORDER BY COALESCE(MAX(e.ts), s.created_at) DESC, COALESCE(MAX(e.id), 0) DESC, s.id DESC"""
        ).fetchall()
        return [{**dict(r), "loop": self.review.get(r["id"])} for r in rows]

    # --- eventos --------------------------------------------------------------------------------------------

    def add_event(
        self, session_id: int, type_: str, payload: dict[str, Any], query_id: str | None = None
    ) -> dict[str, Any]:
        ts = now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (session_id, query_id, type, ts, payload) VALUES (?, ?, ?, ?, ?)",
                (session_id, query_id, type_, ts, json.dumps(payload, default=str, ensure_ascii=False)),
            )
        return {
            "id": cur.lastrowid,
            "session_id": session_id,
            "query_id": query_id,
            "type": type_,
            "ts": ts,
            "payload": payload,
        }

    def query_session(self, query_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT session_id FROM events WHERE query_id = ? AND type = 'query.started'", (query_id,)
        ).fetchone()
        return row[0] if row else None

    def has_event(self, query_id: str, type_: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE query_id = ? AND type = ? LIMIT 1", (query_id, type_)
        ).fetchone()
        return row is not None

    def first_event_of(self, query_id: str, types: tuple[str, ...]) -> str | None:
        marks = ",".join("?" * len(types))
        row = self._conn.execute(
            f"SELECT type FROM events WHERE query_id = ? AND type IN ({marks}) ORDER BY id LIMIT 1", (query_id, *types)
        ).fetchone()
        return row[0] if row else None

    def query_events(self, query_id: str) -> list[dict[str, Any]]:
        """Todos los eventos de una consulta, en orden."""
        rows = self._conn.execute("SELECT * FROM events WHERE query_id = ? ORDER BY id", (query_id,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def manual_events(self, session_id: int, after: int = 0) -> list[dict[str, Any]]:
        """Lo que hizo el ingeniero en el visor después de `after`: consultas manuales (su query.started) y
        consultas fijadas o desfijadas."""
        rows = self._conn.execute(
            """SELECT * FROM events WHERE session_id = ? AND id > ?
                 AND ((type = 'query.started' AND json_extract(payload, '$.origin') = 'manual')
                      OR type IN ('query.pinned', 'query.unpinned'))
               ORDER BY id""",
            (session_id, after),
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def last_agent_event_id(self, session_id: int) -> int:
        """El último evento que dejó el agente en la sesión (nota, borrador, revisión o consulta no manual)."""
        row = self._conn.execute(
            """SELECT COALESCE(MAX(id), 0) FROM events WHERE session_id = ?
                 AND (type IN ('note', 'draft')
                      OR (type = 'query.started' AND COALESCE(json_extract(payload, '$.origin'), 'agent') != 'manual'))""",
            (session_id,),
        ).fetchone()
        return row[0]

    def pinned_queries(self, session_id: int) -> list[str]:
        """Las consultas fijadas de una sesión (la última decisión de cada una), en el orden en que se fijaron."""
        rows = self._conn.execute(
            "SELECT query_id, type FROM events WHERE session_id = ? AND type IN ('query.pinned', 'query.unpinned') "
            "ORDER BY id",
            (session_id,),
        ).fetchall()
        pinned: dict[str, None] = {}
        for r in rows:
            pinned.pop(r["query_id"], None)
            if r["type"] == "query.pinned":
                pinned[r["query_id"]] = None
        return list(pinned)

    def used_entities(self, tools: list[str], key: str, env: str | None, limit: int = 200) -> list[str]:
        """Índices o log groups (params.<key>) ya consultados con esas tools en ese entorno, los más usados primero."""
        marks = ",".join("?" * len(tools))
        rows = self._conn.execute(
            f"""SELECT json_extract(payload, '$.params.{key}') AS name, COUNT(*) AS n FROM events
                WHERE type = 'query.started' AND json_extract(payload, '$.tool') IN ({marks})
                  AND (? IS NULL OR json_extract(payload, '$.params.env') = ?) AND name IS NOT NULL
                GROUP BY name ORDER BY n DESC LIMIT ?""",
            (*tools, env, env, limit),
        ).fetchall()
        return [r["name"] for r in rows]

    def similar_queries(self, tool: str, entity_key: str, entity: str, exclude_session: int | None = None,
                        limit: int = 8) -> list[dict[str, Any]]:
        """Consultas que ya funcionaron con la misma tool sobre la misma entidad (índice, log group o tabla en el
        SQL), de cualquier sesión, las más nuevas primero. `entity_key` es la clave de params ("index",
        "log_group") o "" para buscar la entidad en el texto de la consulta (SQL)."""
        if entity_key:
            cond, arg = f"json_extract(s.payload, '$.params.{entity_key}') = ?", entity
        else:
            cond = "lower(json_extract(s.payload, '$.query')) LIKE ? ESCAPE '\\'"
            arg = "%" + entity.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._conn.execute(
            f"""SELECT s.id, s.session_id, s.query_id, s.ts, s.payload, f.payload AS finished
                FROM events s JOIN events f ON f.query_id = s.query_id AND f.type = 'query.finished'
                WHERE s.type = 'query.started' AND json_extract(s.payload, '$.tool') = ? AND {cond}
                  AND s.session_id != COALESCE(?, -1)
                ORDER BY s.id DESC LIMIT ?""",
            (tool, arg, exclude_session, limit * 3),
        ).fetchall()
        out, seen = [], set()
        for r in rows:
            payload = json.loads(r["payload"])
            key = payload.get("query") or json.dumps(payload.get("args"), sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out.append({"session_id": r["session_id"], "query_id": r["query_id"], "ts": r["ts"],
                        "purpose": payload.get("purpose"), "query": payload.get("query"),
                        "params": payload.get("params"), "args": payload.get("args"),
                        "rows": json.loads(r["finished"]).get("rows")})
            if len(out) >= limit:
                break
        return out

    def search(self, text: str, limit: int = 50) -> list[dict[str, Any]]:
        """Busca en notas, borradores, consultas y resultados de todas las sesiones (LIKE sobre el JSON)."""
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._conn.execute(
            """SELECT e.id, e.session_id, e.query_id, e.type, e.ts, e.payload, s.name, s.card
               FROM events e JOIN sessions s ON s.id = e.session_id
               WHERE e.type IN ('note', 'draft', 'slack', 'query.started', 'query.finished', 'query.failed',
                                'query.rejected')
                 AND e.payload LIKE ? ESCAPE '\\'
               ORDER BY e.id DESC LIMIT ?""",
            (pattern, limit),
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def events_of_type(self, session_id: int, type_: str) -> list[dict[str, Any]]:
        """Los eventos de un tipo en una sesión, en orden (sin traer los resultados del resto de los eventos)."""
        rows = self._conn.execute(
            "SELECT * FROM events WHERE session_id = ? AND type = ? ORDER BY id", (session_id, type_)
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def recent_notes(self, session_id: int, limit: int = 11) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            # Las de la vigilancia de Slack (el mensaje y el análisis) también: son el contexto de la sesión.
            "SELECT id, ts, payload FROM events WHERE session_id = ? AND type IN ('note', 'slack') "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"id": r["id"], "ts": r["ts"], **json.loads(r["payload"])} for r in rows]

    def latest_of_type(self, type_: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM events WHERE type = ? ORDER BY id DESC LIMIT 1", (type_,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def latest_event_id(self) -> int:
        return self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    def events(self, session_id: int | None = None, after: int = 0, limit: int = 500,
               skip_types: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        if skip_types and session_id is not None:
            marks = ", ".join("?" * len(skip_types))
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE session_id = ? AND id > ? AND type NOT IN ({marks}) ORDER BY id LIMIT ?",
                (session_id, after, *skip_types, limit),
            ).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
        if session_id is None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?",
                (session_id, after, limit),
            ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
