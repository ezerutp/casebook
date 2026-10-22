"""Validaciones de solo lectura. Todo lo que no esté explícitamente permitido se rechaza."""

from __future__ import annotations

import re
from typing import Any

MAX_OS_SIZE = 10_000
OS_SERVER_TIMEOUT = "30s"

# Nombres o patrones de índice: letras, dígitos, *, -, _, . y listas separadas por coma. Sin índices de sistema.
_INDEX_PART = re.compile(r"^[a-z0-9*][a-z0-9*_.\-]*$")

# Claves del body que no tienen sentido en una consulta de lectura o que cambian el costo de forma peligrosa.
_FORBIDDEN_BODY_KEYS = {"script_fields", "scroll", "pit", "profile"}


class GuardError(ValueError):
    pass


def check_index(index: str) -> str:
    parts = [p.strip() for p in index.split(",")]
    if not parts or any(not p for p in parts):
        raise GuardError("índice vacío")
    for part in parts:
        if part.startswith((".", "-", "_")) or not _INDEX_PART.match(part):
            raise GuardError(f"índice no permitido: {part!r} (sin índices de sistema, rutas ni parámetros)")
        if part == "*":
            raise GuardError("'*' solo no está permitido: usa un patrón con prefijo, por ejemplo 'leads-*'")
    return ",".join(parts)


def check_search_body(body: dict[str, Any], max_size: int = MAX_OS_SIZE) -> dict[str, Any]:
    forbidden = _FORBIDDEN_BODY_KEYS & body.keys()
    if forbidden:
        raise GuardError(f"claves no permitidas en el body: {sorted(forbidden)}")
    size = body.get("size", 10)
    if not isinstance(size, int) or size < 0 or size > max_size:
        raise GuardError(f"size debe estar entre 0 y {max_size}")
    if body.get("from", 0) + size > max_size:
        raise GuardError(f"from + size no puede superar {max_size}")
    return {**body, "timeout": OS_SERVER_TIMEOUT}


# --- SQL (MySQL) ---------------------------------------------------------------------------------------------

DB_DEFAULT_LIMIT = 100
DB_MAX_LIMIT = 5_000

# Funciones que bloquean, esperan o leen archivos del servidor.
_FORBIDDEN_FUNCS = {
    "SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK", "RELEASE_ALL_LOCKS", "IS_FREE_LOCK", "IS_USED_LOCK",
    "LOAD_FILE", "MASTER_POS_WAIT", "SOURCE_POS_WAIT", "WAIT_FOR_EXECUTED_GTID_SET", "WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS",
}


_EXPLAIN_ANALYZE = re.compile(r"(?is)^\s*(?:/\*.*?\*/\s*)*(?:explain|describe|desc)\s+analyze\b")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _check_query_tree(tree) -> None:
    from sqlglot import exp

    if tree.find(exp.Into):
        raise GuardError("SELECT ... INTO no está permitido")
    for node in tree.find_all(exp.Select, exp.Union, exp.Intersect, exp.Except):
        if node.args.get("locks"):
            raise GuardError("FOR UPDATE / LOCK IN SHARE MODE no están permitidos")
    for func in tree.find_all(exp.Func):
        name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
        if name.upper() in _FORBIDDEN_FUNCS:
            raise GuardError(f"la función {name.upper()} no está permitida")


def check_sql(sql: str, limit: int = DB_DEFAULT_LIMIT, max_limit: int = DB_MAX_LIMIT) -> tuple[str, str]:
    """Devuelve (tipo, sql a ejecutar). tipo: "select", "show" o "describe" (DESCRIBE tabla o EXPLAIN consulta).
    A los SELECT se les fuerza LIMIT: se agrega si falta y se rechaza si supera max_limit."""
    import sqlglot
    from sqlglot import exp

    if not 1 <= limit <= max_limit:
        raise GuardError(f"el límite de filas tiene que estar entre 1 y {max_limit}")
    if _EXPLAIN_ANALYZE.match(sql):
        raise GuardError("EXPLAIN ANALYZE ejecuta la consulta; usa EXPLAIN a secas")
    try:
        statements = [s for s in sqlglot.parse(sql, read="mysql") if s is not None]
    except sqlglot.errors.ParseError as exc:
        detail = _ANSI.sub("", str(exc)).splitlines()[0]
        raise GuardError(f"no se pudo validar el SQL (solo se acepta SELECT, SHOW, DESCRIBE o EXPLAIN): {detail}") from exc
    if len(statements) != 1:
        raise GuardError("se acepta exactamente una sentencia")
    tree = statements[0]

    if isinstance(tree, exp.Show):
        return "show", sql.strip().rstrip(";")
    if isinstance(tree, exp.Describe):
        inner = tree.this
        if isinstance(inner, exp.Query):
            _check_query_tree(inner)
        elif not isinstance(inner, exp.Table):
            raise GuardError("DESCRIBE/EXPLAIN solo sobre una tabla o una consulta SELECT")
        return "describe", sql.strip().rstrip(";")
    if not isinstance(tree, exp.Query):
        raise GuardError(f"solo lectura: {type(tree).__name__.upper()} no está permitido (usa SELECT, SHOW, DESCRIBE o EXPLAIN)")

    _check_query_tree(tree)
    current = tree.args.get("limit")
    if current is None:
        tree = tree.limit(limit)
    else:
        value = current.expression if isinstance(current, exp.Limit) else current
        if not (isinstance(value, exp.Literal) and value.is_int):
            raise GuardError("LIMIT tiene que ser un número literal")
        if int(value.this) > max_limit:
            raise GuardError(f"LIMIT {value.this} supera el máximo de {max_limit}")
    return "select", tree.sql(dialect="mysql")
