"""Piezas compartidas por las tools."""

from __future__ import annotations

import functools
import inspect
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import config, guards
from ..backends.errors import BackendError
from ..config import Config
from ..events import NoActiveSession, Recorder, current_purpose

Purpose = Annotated[
    str,
    Field(
        description="Para qué es esta consulta, en una frase corta en español. Se muestra como título en el visor "
        "del ingeniero. Ejemplos: 'Buscar al usuario por email', 'Ver si el envío a la integración falló'."
    ),
]


def expected_errors(recorder: Recorder, cfg: Config | None = None):
    """Los errores esperados llegan a Claude con su mensaje (el SDK oculta el texto de cualquier otra excepción).
    Un guard que bloquea la llamada antes de que empiece una consulta queda en el timeline como "rechazada".
    Con `cfg`, antes de cada llamada se toman los cambios de config.toml (por ejemplo, guardados en el visor)."""

    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            if cfg is not None:
                config.refresh(cfg)
            started = recorder.queries_started
            token = current_purpose.set((kwargs.pop("purpose", None) or "").strip() or None)
            try:
                return await fn(*args, **kwargs)
            except guards.GuardError as exc:
                if recorder.queries_started == started and recorder.session_id is not None:
                    recorder.reject(fn.__name__, kwargs, str(exc))
                raise ToolError(str(exc)) from exc
            except (BackendError, NoActiveSession) as exc:
                raise ToolError(str(exc)) from exc
            finally:
                current_purpose.reset(token)

        # La firma que ve el SDK incluye `purpose` (la tool no lo recibe: lo consume el wrapper).
        sig = inspect.signature(fn, eval_str=True)  # las anotaciones son strings (from __future__)
        params = list(sig.parameters.values())
        purpose = inspect.Parameter("purpose", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Purpose)
        first_default = next((i for i, prm in enumerate(params) if prm.default is not inspect.Parameter.empty), len(params))
        params.insert(first_default, purpose)
        wrapper.__signature__ = sig.replace(parameters=params)
        wrapper.__annotations__ = {
            **{name: prm.annotation for name, prm in sig.parameters.items()},
            "purpose": Purpose,
            "return": sig.return_annotation,
        }
        return wrapper

    return decorate
