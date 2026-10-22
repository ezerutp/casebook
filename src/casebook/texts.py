"""Textos para Claude que dependen del pack: de qué son los casos, ejemplos de card y qué hay en cada fuente.

Las plantillas usan ${nombre} (string.Template), así las llaves de los ejemplos JSON de los docstrings no molestan:
- ${domain}: de qué son los casos, por ejemplo "casos de soporte" ([texts] domain);
- ${card} y ${card2}: dos ejemplos de card ([card] examples);
- ${database}: el nombre de la base.
"""

from __future__ import annotations

import inspect
from string import Template

from .config import Config


def values(cfg: Config) -> dict[str, str]:
    return {
        "domain": cfg.texts_domain,
        "card": cfg.card_examples[0],
        "card2": cfg.card_examples[1],
        "database": cfg.db.database or "la base",
    }


def render(text: str, cfg: Config) -> str:
    return Template(inspect.cleandoc(text or "")).safe_substitute(values(cfg))


def tool(mcp, cfg: Config, extra: str = ""):
    """Como @mcp.tool(), con la descripción (el docstring) pasada por render() y `extra` al final."""

    def decorate(fn):
        description = render(fn.__doc__, cfg) + (f"\n{extra}" if extra else "")
        return mcp.tool(description=description.strip())(fn)

    return decorate
