"""Conocimiento local, independiente del historial. Ninguna operación consulta producción."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redact import Redactor
from .store import now

SOURCES = {"mysql", "opensearch", "cloudwatch", "code"}
STATUSES = {"verified", "pending", "stale"}
KINDS = {"schema", "catalog", "mapping", "relation", "query", "reference", "limitation"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, environment TEXT NOT NULL,
 entity TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
 topics TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, verified_at TEXT,
 revision INTEGER NOT NULL, signature TEXT, UNIQUE(source, environment, entity, kind)
);
CREATE TABLE IF NOT EXISTS revisions (
 entry_id INTEGER NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
 PRIMARY KEY(entry_id, revision)
);
CREATE TABLE IF NOT EXISTS observations (key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS deleted_entries (id INTEGER PRIMARY KEY);
PRAGMA user_version=1;
"""


FORMAT = "casebook_knowledge"  # la clave que identifica un archivo exportado; su valor es la versión
MAX_IMPORT = 5000
SIGNED = ("source", "environment", "entity", "kind", "title", "content", "topics")


class KnowledgeError(ValueError):
    pass


class Conflict(KnowledgeError):
    pass


def _text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise KnowledgeError(f"{name}: texto requerido, máximo {maximum} caracteres")
    return value.strip()


def signature(entry: dict) -> str:
    """Firma de una entrada: identifica su contenido en cualquier máquina. Deja fuera estado, fechas y evidencia,
    que son locales. No es criptográfica contra nadie: solo sirve para no importar dos veces lo mismo."""
    data = [entry[k] for k in SIGNED[:-1]] + [sorted(entry["topics"])]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]


def _clean(source, environment, entity, kind, title, content, topics) -> dict:
    """Los campos firmados tal como quedan guardados: validados, recortados y redactados."""
    if source not in SOURCES or kind not in KINDS:
        raise KnowledgeError("fuente, tipo o estado inválido")
    if not isinstance(topics, (list, tuple)) or len(topics) > 20:
        raise KnowledgeError("máximo 20 temas")
    redactor = Redactor(mask_emails=True)  # la memoria compartida no conserva emails de clientes
    return {"source": source, "environment": _text(environment, "entorno", 200),
            "entity": _text(entity, "entidad", 300), "kind": kind,
            "title": redactor.text(_text(title, "título", 200)),
            "content": redactor.text(_text(content, "contenido", 100_000)),
            "topics": sorted({_text(t, "tema", 60).casefold() for t in topics})}


def _stamp(value) -> str | None:
    """Una fecha de un archivo importado, en el formato de now(). Una fecha futura no puede ganarle a lo local."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise KnowledgeError("fecha inválida") from None
    if parsed.tzinfo is None:
        raise KnowledgeError("fecha sin zona horaria")
    return min(parsed.astimezone(UTC).isoformat(timespec="milliseconds"), now())


def parse_export(data: Any) -> tuple[list, str | None]:
    """(entradas, exported_at) de un archivo exportado; también acepta la lista simple de knowledge/*.json de un pack."""
    exported_at = None
    if isinstance(data, dict) and FORMAT in data:
        if data[FORMAT] != 1:
            raise KnowledgeError("el archivo es de una versión más nueva de casebook: actualiza la app")
        exported_at, data = data.get("exported_at"), data.get("entries")
    if not isinstance(data, list):
        raise KnowledgeError("no es un archivo de conocimiento de casebook")
    if len(data) > MAX_IMPORT:
        raise KnowledgeError(f"máximo {MAX_IMPORT} entradas por archivo")
    return data, exported_at if isinstance(exported_at, str) else None


def _decode(row) -> dict:
    d = dict(row)
    for key in ("topics", "evidence"):
        d[key] = json.loads(d[key])
    return d


class KnowledgeStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise KnowledgeError("knowledge.db pertenece a una versión más nueva")
            c.executescript(SCHEMA)
            if not any(r[1] == "signature" for r in c.execute("PRAGMA table_info(entries)")):
                c.execute("ALTER TABLE entries ADD COLUMN signature TEXT")  # bases anteriores a las firmas
            yield c
        finally:
            c.close()

    def search(self, query="", source=None, environment=None, topic=None, status=None, limit=20, offset=0):
        if len(query) > 500:
            raise KnowledgeError("búsqueda demasiado larga")
        clauses, args = [], []
        for key, value in (("source", source), ("environment", environment), ("status", status)):
            if value:
                clauses.append(f"{key} = ?")
                args.append(value)
        if topic:
            clauses.append("EXISTS (SELECT 1 FROM json_each(entries.topics) WHERE value = ?)")
            args.append(topic.casefold())
        # Palabras combinadas con AND; LIKE literal, sin comodines proporcionados por el usuario.
        for word in query.casefold().split():
            clauses.append("lower(entity || ' ' || title || ' ' || content || ' ' || topics) LIKE ? ESCAPE '\\'")
            args.append("%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit, offset = max(1, min(limit, 50)), max(0, offset)
        with self.connection() as c:
            total = c.execute("SELECT count(*) FROM entries" + where, args).fetchone()[0]
            rows = c.execute("SELECT id, source, environment, entity, kind, title, topics, status, updated_at, verified_at, revision, substr(content,1,240) AS summary FROM entries" + where + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        items = [dict(r) for r in rows]
        for item in items:
            item["topics"] = json.loads(item["topics"])
        return {"entries": items, "total": total, "next_offset": offset + limit if offset + limit < total else None}

    def read(self, entry_id: int, history_offset: int = 0, revision: int | None = None):
        with self.connection() as c:
            row = c.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if row is None:
                raise KnowledgeError("entrada no encontrada")
            result = _decode(row)
            if revision is not None:
                snapshot = c.execute("SELECT snapshot FROM revisions WHERE entry_id=? AND revision=?", (entry_id, revision)).fetchone()
                if snapshot is None:
                    raise KnowledgeError("revisión no encontrada")
                result = json.loads(snapshot[0])
            offset = max(0, history_offset)
            history = c.execute("SELECT snapshot FROM revisions WHERE entry_id=? ORDER BY revision DESC LIMIT 11 OFFSET ?", (entry_id, offset)).fetchall()
        result["history"] = [{k: v for k, v in json.loads(r[0]).items() if k not in {"content", "evidence"}} for r in history[:10]]
        result["next_history_offset"] = offset + 10 if len(history) > 10 else None
        return result

    def write(self, *, source: str, environment: str, entity: str, kind: str, title: str,
              content: str, topics: list[str], evidence: dict, status="pending", reason: str,
              expected_revision: int = 0, actor="agent", observed_at=None, observation=None):
        if status not in STATUSES:
            raise KnowledgeError("fuente, tipo o estado inválido")
        clean = _clean(source, environment, entity, kind, title, content, topics)
        environment, entity, title, content, topics = (clean[k] for k in SIGNED[1:3] + SIGNED[4:])
        if not isinstance(evidence, dict) or not evidence or len(json.dumps(evidence)) > 8000:
            raise KnowledgeError("evidencia requerida (máximo 8000 caracteres)")
        redactor = Redactor(mask_emails=True)
        reason = redactor.text(_text(reason, "motivo", 1000))
        evidence = redactor.value(evidence)
        stamp = now()
        with self.connection() as c, c:
            c.execute("BEGIN IMMEDIATE")
            old_row = c.execute("SELECT * FROM entries WHERE source=? AND environment=? AND entity=? AND kind=?", (source, environment, entity, kind)).fetchone()
            old = _decode(old_row) if old_row else None
            if observation and c.execute("SELECT 1 FROM observations WHERE key=?", (observation,)).fetchone():
                return {"id": old["id"] if old else None, "changed": False}
            if actor not in {"capture", "import"} and (old["revision"] if old else 0) != expected_revision:
                raise Conflict("la entrada cambió o ya existe; vuelve a leerla antes de guardar")
            if observed_at and old and observed_at < old["updated_at"]:
                if observation:
                    c.execute("INSERT OR IGNORE INTO observations VALUES (?)", (observation,))
                return {"id": old["id"], "changed": False}
            if actor == "capture" and old:
                topics = sorted(set(topics) | set(old["topics"]))
            revision = old["revision"] + 1 if old else 1
            verified_at = (observed_at or stamp) if status == "verified" else (old["verified_at"] if old else None)
            updated_at = observed_at or stamp
            # IDs retirados no se reutilizan: los enlaces históricos no deben apuntar a otra entrada.
            entry_id = old["id"] if old else c.execute(
                "SELECT coalesce(max(id),0)+1 FROM (SELECT id FROM entries UNION ALL SELECT id FROM deleted_entries)"
            ).fetchone()[0]
            values = (entry_id, source, environment, entity, kind, title, content, json.dumps(topics), status,
                      json.dumps(evidence, ensure_ascii=False), old["created_at"] if old else updated_at,
                      updated_at, verified_at, revision, signature({**clean, "topics": topics}))
            c.execute("""INSERT INTO entries(id,source,environment,entity,kind,title,content,topics,status,evidence,created_at,updated_at,verified_at,revision,signature)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,environment,entity,kind) DO UPDATE SET
                title=excluded.title,content=excluded.content,topics=excluded.topics,status=excluded.status,
                evidence=excluded.evidence,updated_at=excluded.updated_at,verified_at=excluded.verified_at,
                revision=excluded.revision,signature=excluded.signature""", values)
            entry = _decode(c.execute("SELECT * FROM entries WHERE source=? AND environment=? AND entity=? AND kind=?", (source, environment, entity, kind)).fetchone())
            snapshot = {**entry, "reason": reason, "actor": actor}
            c.execute("INSERT INTO revisions VALUES (?,?,?)", (entry["id"], revision, json.dumps(snapshot, ensure_ascii=False)))
            if observation:
                c.execute("INSERT INTO observations VALUES (?)", (observation,))
        return {"id": entry["id"], "revision": revision, "changed": True, "status": status}

    def delete(self, entry_id: int, expected_revision: int):
        with self.connection() as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT revision FROM entries WHERE id=?", (entry_id,)).fetchone()
            if row is None:
                raise KnowledgeError("entrada no encontrada")
            if row["revision"] != expected_revision:
                raise Conflict("la entrada cambió; vuelve a leerla antes de eliminar")
            c.execute("INSERT INTO deleted_entries(id) VALUES (?)", (entry_id,))
            c.execute("DELETE FROM revisions WHERE entry_id=?", (entry_id,))
            c.execute("DELETE FROM entries WHERE id=?", (entry_id,))
            # Mantener recibos evita que importar las mismas consultas restaure lo eliminado.
        return {"deleted": True, "id": entry_id}

    def export(self) -> dict:
        """Todas las entradas, firmadas, para compartir. Firma primero las que aún no tienen firma."""
        with self.connection() as c, c:
            c.execute("BEGIN IMMEDIATE")
            for row in c.execute("SELECT * FROM entries WHERE signature IS NULL").fetchall():
                c.execute("UPDATE entries SET signature=? WHERE id=?", (signature(_decode(row)), row["id"]))
            rows = [_decode(r) for r in c.execute("SELECT * FROM entries ORDER BY source, environment, entity, kind")]
        # Solo el resumen de la evidencia: los IDs de sesión y consulta apuntan a un historial que no viaja.
        entries = [{"signature": r["signature"], **{k: r[k] for k in SIGNED}, "status": r["status"],
                    "updated_at": r["updated_at"], "evidence_summary": str(r["evidence"].get("summary", ""))[:2000]}
                   for r in rows]
        return {FORMAT: 1, "exported_at": now(), "entries": entries}

    def import_entries(self, data: Any, *, origin: str, actor="import", status: str | None = None,
                       dry_run: bool = False) -> dict:
        """Importa un archivo exportado (o la lista semilla de un pack) sin duplicar. La firma se recalcula: no se
        confía en la del archivo. Una firma ya vista se omite, aunque la entrada se haya editado o eliminado aquí.
        Si la entidad ya existe con otro contenido gana la más reciente; una entrada sin fecha nunca pisa la local.
        `status` fuerza el estado de lo importado (las semillas entran como pending). `dry_run` solo cuenta.
        `items` lista (hasta 50) las entradas nuevas o actualizadas, para revisarlas o enlazarlas."""
        items, exported_at = parse_export(data)
        result = {"new": 0, "existing": 0, "updated": 0, "kept": 0, "invalid": 0}
        changed: list[dict] = []
        planned: set[str] = set()

        def note(change, entry_id, clean):
            if len(changed) < 50:
                changed.append({"id": entry_id, "change": change, "title": clean["title"], "entity": clean["entity"],
                                "source": clean["source"], "environment": clean["environment"], "kind": clean["kind"]})
        for item in items:
            try:
                clean = _clean(*(item[k] for k in SIGNED[:-1]), item.get("topics", []))
                observed_at = _stamp(item.get("updated_at"))
                state = status or item.get("status", "pending")
                summary = item.get("evidence_summary")
                evidence = {"summary": (summary if isinstance(summary, str) and summary.strip() else "Sin resumen")[:2000],
                            "imported_from": origin, **({"exported_at": exported_at} if exported_at else {})}
            except (KeyError, TypeError, AttributeError, KnowledgeError):
                result["invalid"] += 1
                continue
            sig, receipt = signature(clean), f"sig:{signature(clean)}"
            with self.connection() as c:
                seen = c.execute("SELECT 1 FROM observations WHERE key=?", (receipt,)).fetchone()
                row = c.execute("SELECT * FROM entries WHERE source=? AND environment=? AND entity=? AND kind=?",
                                [clean[k] for k in SIGNED[:4]]).fetchone()
            old = _decode(row) if row else None
            if seen or sig in planned or (old and (old["signature"] or signature(old)) == sig):
                result["existing"] += 1
                continue
            if old and (observed_at is None or observed_at <= old["updated_at"]):
                result["kept"] += 1
                continue
            if dry_run:
                planned.add(sig)
                result["updated" if old else "new"] += 1
                note("updated" if old else "new", old["id"] if old else None, clean)
                continue
            try:
                saved = self.write(**clean, evidence=evidence, status=state, actor=actor, observed_at=observed_at,
                                   observation=receipt, reason=f"Importado de {origin}"[:1000])
            except KnowledgeError:
                result["invalid"] += 1
                continue
            # Otro proceso pudo guardar lo mismo entre la comprobación y la escritura.
            change = ("updated" if old else "new") if saved["changed"] else "existing"
            result[change] += 1
            if saved["changed"]:
                note(change, saved["id"], clean)
        return {**result, "items": changed}

    def edit(self, entry_id, *, expected_revision, title, content, topics, reason, stale=False):
        old = self.read(entry_id)
        return self.write(**{k: old[k] for k in ("source", "environment", "entity", "kind", "evidence")},
                          title=title, content=content, topics=topics, reason=reason,
                          status="stale" if stale else "pending", expected_revision=expected_revision)


class Knowledge:
    """Servicio compartido por MCP, captura y visor; apertura diferida para no bloquear soporte."""
    def __init__(self, sessions):
        self.sessions = sessions
        if str(sessions.path) == ":memory:":
            import tempfile
            self._temp = tempfile.TemporaryDirectory(prefix="casebook-knowledge-")
            path = Path(self._temp.name) / "knowledge.db"
        else:
            path = sessions.path.with_name("knowledge.db")
        self.db = KnowledgeStore(path)

    def record(self, *, query_id=None, code_path=None, code_revision=None, evidence_summary: str, **fields):
        evidence = {"summary": _text(evidence_summary, "resumen de evidencia", 2000)}
        if query_id:
            sid = self.sessions.query_session(query_id)
            if sid is None or not self.sessions.has_event(query_id, "query.finished"):
                raise KnowledgeError("query_id debe identificar una consulta exitosa guardada")
            evidence.update(session_id=sid, query_id=query_id)
        elif code_path and code_revision:
            evidence.update(code_path=_text(code_path, "archivo", 500), code_revision=_text(code_revision, "revisión", 100))
        else:
            raise KnowledgeError("indica query_id exitoso o archivo y revisión del código")
        # Una conclusión escrita por el agente sigue pendiente, incluso con referencia válida.
        return self.db.write(**fields, evidence=evidence, status="pending")

    def capture(self, start: dict, end: dict):
        tool, params = start["payload"]["tool"], start["payload"].get("params", {})
        result = end["payload"].get("result", {})
        rows, columns = result.get("rows", []), result.get("columns", [])
        source, env, entity, kind, content = None, None, None, None, None
        if tool == "db_tables":
            source, env, entity, kind = "mysql", params.get("environment", "sin-entorno"), params.get("pattern", "%"), "catalog"
            content = json.dumps({"pattern": entity, "scope": "solo tablas que coinciden con este patrón en la fecha indicada", "tables": [r[0] for r in rows]}, ensure_ascii=False)
        elif tool == "db_describe":
            source, env, entity, kind = "mysql", params.get("environment", "sin-entorno"), params["table"], "schema"
            content = json.dumps({"columns": [dict(zip(columns, r)) for r in rows], "indexes": result.get("tables", [])}, ensure_ascii=False)
        elif tool == "os_mapping":
            source, env, kind = "opensearch", params.get("env", "sin-entorno"), "mapping"
            entity = end["payload"].get("meta", {}).get("from_index")
            if not entity:
                return []
            content = json.dumps({"requested_pattern": params.get("index"), "scope": "solo este índice concreto; no representa todos los índices del patrón", "fields": dict(rows)}, ensure_ascii=False)
        else:
            return []
        evidence = {"session_id": start["session_id"], "query_id": start["query_id"], "summary": f"{tool} completada; metadatos de {entity}"}
        entry = self.db.write(source=source, environment=env, entity=entity, kind=kind,
                              title=f"{entity} · {kind}", content=content, topics=[], evidence=evidence,
                              status="verified", reason="Captura automática de metadatos exitosos",
                              actor="capture", observed_at=end["ts"], observation=f"{start['query_id']}:{kind}:{entity}")
        return [entry] if entry["changed"] else []

    def contradiction(self, start: dict, end: dict):
        if start["payload"]["tool"] not in {"db_select", "db_describe"}:
            return []
        error = end["payload"].get("error", "")
        missing = re.search(r"Table '([^']+)\.([^']+)' doesn't exist", error)
        unknown = re.search(r"Unknown column '[^']+'", error)
        if not missing and not unknown:
            return []
        params = start["payload"].get("params", {})
        env = params.get("environment", "sin-entorno")
        entities = {missing[2]} if missing else set()
        if unknown:
            import sqlglot
            from sqlglot import exp
            try:
                tree = sqlglot.parse_one(start["payload"].get("query", ""), read="mysql")
                entities = {t.name for t in tree.find_all(exp.Table)}
            except sqlglot.errors.ParseError:
                return []
        updated = []
        for entity in entities:
            with self.db.connection() as c:
                row = c.execute("SELECT id FROM entries WHERE source='mysql' AND environment=? AND entity=? AND kind='schema'", (env, entity)).fetchone()
            if not row and missing:
                updated.append(self.db.write(source="mysql", environment=env, entity=f"missing-table:{entity}",
                    kind="limitation", title=f"Tabla no encontrada: {entity}",
                    content=f"La consulta reportó que {entity} no existía en este entorno y fecha. No prueba su ausencia permanente ni identifica la tabla correcta.",
                    topics=[], evidence={"session_id": start["session_id"], "query_id": start["query_id"], "summary": error},
                    status="pending", reason="Observación de error de esquema", actor="capture", observed_at=end["ts"],
                    observation=f"{start['query_id']}:missing:{entity}"))
            if row:
                old = self.db.read(row[0])
                # Una observación histórica no invalida información comprobada después.
                if end["ts"] < old["updated_at"]:
                    continue
                updated.append(self.db.write(**{k: old[k] for k in ("source", "environment", "entity", "kind", "title", "content", "topics")},
                    evidence={"session_id": start["session_id"], "query_id": start["query_id"], "summary": error},
                    status="stale", reason="Error de esquema: verificar la entrada y la consulta", actor="capture",
                    observed_at=end["ts"], observation=f"{start['query_id']}:stale:{entity}"))
        return [e for e in updated if e["changed"]]

    def observe(self, start, end):
        try:
            entries = self.capture(start, end) if end["type"] == "query.finished" else self.contradiction(start, end)
            return {"entries": entries} if entries else {}
        except (OSError, sqlite3.Error, KnowledgeError, KeyError, TypeError, ValueError):
            return {"warning": "No se pudo actualizar conocimiento; la consulta y su evidencia siguen disponibles."}

    def import_sessions(self):
        starts, cursor, changed, warnings = {}, 0, 0, 0
        while batch := self.sessions.events(after=cursor, limit=500):
            for event in batch:
                cursor = event["id"]
                if event["type"] == "query.started":
                    starts[event["query_id"]] = event
                elif event["type"] in {"query.finished", "query.failed"}:
                    start = starts.pop(event["query_id"], None)
                    if start:
                        outcome = self.observe(start, event)
                        changed += len(outcome.get("entries", []))
                        warnings += bool(outcome.get("warning"))
        return {"changed": changed, "warnings": warnings}
