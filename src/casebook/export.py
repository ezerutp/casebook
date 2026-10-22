"""Exporta una sesión a Markdown: notas, consultas (texto + tabla recortada), errores y borradores para Jira."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .store import Store

EXPORT_DIR = DATA_DIR / "exports"
CELL_MAX = 120

_LANG = {"db_": "sql", "os_": "json"}
SERVICES = {"os_": "OpenSearch", "cw_": "CloudWatch", "db_": "MySQL"}


def service(tool: str) -> str | None:
    return next((name for prefix, name in SERVICES.items() if tool.startswith(prefix)), None)


def _time(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")


def _cell(v: Any) -> str:
    if v is None:
        return ""
    text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    text = text.replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= CELL_MAX else text[:CELL_MAX] + "…"


def table(columns: list[str], rows: list[list[Any]], max_rows: int) -> str:
    if not columns:
        return "_Sin columnas._"
    lines = ["| " + " | ".join(_cell(c) for c in columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(_cell(v) for v in r) + " |" for r in rows[:max_rows]]
    if len(rows) > max_rows:
        lines.append(f"\n_{len(rows) - max_rows} filas más en el visor._")
    if not rows:
        lines.append(f"| {' | '.join('' for _ in columns)} |")
        lines.append("\n_Sin filas._")
    return "\n".join(lines)


def _code(tool: str, text: str) -> str:
    lang = next((v for k, v in _LANG.items() if tool.startswith(k)), "")
    if lang == "json" and "\n" in text:
        lang = ""  # "GET path\n{body}" no es JSON válido
    fence = "````" if "```" in text else "```"
    return f"{fence}{lang}\n{text}\n{fence}"


def _meta(p: dict[str, Any]) -> str:
    meta = p.get("meta") or {}
    parts = [f"{p.get('rows', 0)} filas", f"{p.get('duration_ms', 0)} ms"]
    if meta.get("env"):
        parts.append(meta["env"])
    if meta.get("total_hits") is not None:
        parts.append(f"{meta['total_hits']} hits totales")
    if meta.get("cost_usd") is not None:
        parts.append(f"${float(meta['cost_usd']):.4f}")
    elif meta.get("estimated_cost_usd") is not None:
        parts.append(f"estimado ${float(meta['estimated_cost_usd']):.4f}")
    if meta.get("truncated"):
        parts.append("truncado por LIMIT")
    return " · ".join(parts)


def render(store: Store, session_id: int, max_rows: int = 20) -> str:
    session = store.get_session(session_id)
    if not session:
        raise ValueError(f"no existe la sesión {session_id}")
    events = store.events(session_id, limit=100_000)

    queries: dict[str, dict[str, Any]] = {}
    order: list[tuple[str, Any]] = []
    drafts: dict[str, list[dict[str, Any]]] = {}
    health: dict[str, Any] | None = None
    total_cost = 0.0
    for ev in events:
        p = ev["payload"]
        if ev["type"] == "note":
            order.append(("note", ev))
        elif ev["type"] == "slack":
            order.append(("slack", ev))
        elif ev["type"] == "draft":
            drafts.setdefault(p["card"].strip().upper(), []).append(ev)
        elif ev["type"] == "health" and health is None:
            health = ev
        elif ev["type"] == "query.rejected":
            order.append(("rejected", ev))
        elif ev["type"] in ("query.approved", "query.denied") and ev["query_id"] in queries:
            queries[ev["query_id"]]["decision"] = ev["type"]
        elif ev["type"] == "query.started":
            queries[ev["query_id"]] = {"start": ev, "end": None, "decision": None}
            order.append(("query", ev["query_id"]))
        elif ev["type"] in ("query.finished", "query.failed") and ev["query_id"] in queries:
            queries[ev["query_id"]]["end"] = ev
            cost = (p.get("meta") or {}).get("cost_usd")
            if cost:
                total_cost += float(cost)

    n_queries = len(queries)
    n_failed = sum(1 for q in queries.values() if q["end"] and q["end"]["type"] == "query.failed")
    n_rejected = sum(1 for kind, _ in order if kind == "rejected")
    created = datetime.fromisoformat(session["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
    title = session["name"] + (f" ({session['card']})" if session["card"] and session["card"] != session["name"] else "")

    out = [f"# {title}", ""]
    summary = f"Sesión {session_id} · {created} · {n_queries} consultas"
    if n_failed:
        summary += f" ({n_failed} con error)"
    if n_rejected:
        summary += f" · {n_rejected} rechazadas por los guards"
    if total_cost:
        summary += f" · CloudWatch ${total_cost:.4f}"
    out += [summary, ""]

    per_service: dict[str, dict[str, int]] = {}
    for q in queries.values():
        name = service(q["start"]["payload"]["tool"])
        if name:
            stats = per_service.setdefault(name, {"total": 0, "error": 0, "rejected": 0})
            stats["total"] += 1
            stats["error"] += int(bool(q["end"]) and q["end"]["type"] == "query.failed")
    for kind, item in order:
        name = service(item["payload"]["tool"]) if kind == "rejected" else None
        if name:
            stats = per_service.setdefault(name, {"total": 0, "error": 0, "rejected": 0})
            stats["total"] += 1
            stats["rejected"] += 1
    if per_service:
        out += ["| Servicio | Llamadas | Con error | Rechazadas |", "|---|---|---|---|"]
        for name in SERVICES.values():
            if name in per_service:
                st = per_service[name]
                out.append(f"| {name} | {st['total']} | {st['error']} | {st['rejected']} |")
        out.append("")

    if health:
        marks = {"ok": "✓", "warn": "!", "fail": "✗"}
        out += ["**Conexiones al abrir la sesión**", ""]
        out += [f"- {marks.get(c['status'], '?')} `{c['name']}`: {c['detail']}" for c in health["payload"]["checks"]]
        out.append("")

    # Sin notas de Claude, la conclusión de una sesión de la vigilancia de Slack es su análisis.
    conclusions = [ev for kind, ev in order if kind == "note"] or [
        ev for kind, ev in order if kind == "slack" and ev["payload"].get("kind") == "analysis"]
    if conclusions:
        out += ["## Conclusión (última nota)", "", conclusions[-1]["payload"]["text"], ""]

    out += ["## Timeline", ""]
    for kind, item in order:
        if kind == "note":
            out += [f"**Nota · {_time(item['ts'])}**", "", item["payload"]["text"], ""]
            continue
        if kind == "slack":
            sp = item["payload"]
            if sp.get("kind") == "message":
                covers = len(sp.get("covers") or [])
                who = (f"Respuestas nuevas en el hilo ({covers})" if covers > 1
                       else f"{'Repregunta' if sp.get('follow_up') else 'Mensaje'} de {sp.get('author')}")
                out += [f"**Slack · {who} · {_time(item['ts'])}**", "",
                        "> " + sp["text"].replace("\n", "\n> "), ""]
            else:
                label = {"analysis": "Análisis", "skipped": "Sin analizar", "error": "Análisis fallido"}
                out += [f"**Slack · {label.get(sp.get('kind'), 'Slack')} · {_time(item['ts'])}**", "", sp["text"], ""]
            continue
        if kind == "rejected":
            rp = item["payload"]
            title = f" · {rp['purpose']}" if rp.get("purpose") else ""
            out += [f"### {_time(item['ts'])} · `{rp['tool']}`{title} · rechazada", ""]
            text = (rp.get("params") or {}).get("sql") or (rp.get("params") or {}).get("query")
            if text:
                out += [_code(rp["tool"], str(text)), ""]
            out += [f"> Rechazada antes de ejecutarse: {rp.get('reason')}", ""]
            continue
        q = queries[item]
        start, end = q["start"], q["end"]
        sp = start["payload"]
        status = "corriendo" if end is None else ("ok" if end["type"] == "query.finished" else "error")
        title = f" · {sp['purpose']}" if sp.get("purpose") else ""
        heading = f"### {_time(start['ts'])} · `{sp['tool']}`{title} · {status}"
        if end is not None and end["type"] == "query.finished":
            heading += f" · {_meta(end['payload'])}"
        if q["decision"] == "query.approved":
            heading += " · aprobada a mano"
        elif q["decision"] == "query.denied":
            heading += " · no aprobada"
        out += [heading, ""]
        if sp.get("query"):
            out += [_code(sp["tool"], sp["query"]), ""]
        if end is None:
            continue
        ep = end["payload"]
        if end["type"] == "query.failed":
            out += [f"> Error: {ep.get('error')}", ""]
            continue
        result = ep.get("result") or {}
        out += [table(result.get("columns", []), result.get("rows", []), max_rows), ""]
        for extra in result.get("tables") or []:
            out += [f"**{extra.get('name')}**", "", table(extra.get("columns", []), extra.get("rows", []), max_rows), ""]

    if drafts:
        out += ["## Borradores de comentarios para Jira", ""]
        for card, versions in drafts.items():
            d = versions[-1]
            out += [f"### {card} · {_time(d['ts'])}", "", d["payload"]["text"], ""]
            if len(versions) > 1:
                out += [f"#### Historial · {len(versions)} versiones (actual: {len(versions)})", ""]
                for number, previous in reversed(list(enumerate(versions[:-1], 1))):
                    out += [f"**Versión {number} · {previous['ts']}**", "", previous["payload"]["text"], ""]

    return "\n".join(out).rstrip() + "\n"


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "sesion"


def write(store: Store, session_id: int, path: Path | None = None, max_rows: int = 20) -> Path:
    session = store.get_session(session_id)
    if not session:
        raise ValueError(f"no existe la sesión {session_id}")
    path = path or EXPORT_DIR / f"{slug(session['card'] or session['name'])}-{session_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(store, session_id, max_rows))
    return path
