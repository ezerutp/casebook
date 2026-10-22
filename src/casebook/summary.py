"""Lo que recibe Claude: un resumen corto y JSON compacto. El resultado completo queda en el visor."""

from __future__ import annotations

import json
from typing import Any

ROWS_FOR_CLAUDE = 25
CELL_MAX = 300


def _cell(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, str) and len(v) > CELL_MAX:
        return v[:CELL_MAX] + f"… (+{len(v) - CELL_MAX} chars)"
    return v


def table(columns: list[str], rows: list[list[Any]], limit: int = ROWS_FOR_CLAUDE) -> dict[str, Any]:
    out: dict[str, Any] = {"columns": columns, "rows": [[_cell(c) for c in r] for r in rows[:limit]]}
    if len(rows) > limit:
        out["omitted_rows"] = len(rows) - limit
    return out


def render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
