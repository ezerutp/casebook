"""Proveedores: el código que sabe hablar con un tipo de servicio (OpenSearch, logs, SQL).

Una fuente de config.toml (un entorno de OpenSearch, CloudWatch, la base) elige su proveedor con `provider = "…"`.
Cada proveedor declara:

- `kind`: "opensearch", "logs" o "sql";
- `name`: el valor de `provider` en config.toml;
- `auth`: de dónde sale la sesión: "browser" (cookie del navegador: login y extensión), "secret" (usuario y
  contraseña o API key en el keyring, con `login <id>`), "external" (la maneja otra herramienta: perfil de AWS,
  archivo de credenciales) o "none";
- `fields`: el esquema de sus opciones. De ahí salen la validación de config.toml y, más adelante, los formularios
  del asistente y del visor.

Los internos están en BUILTIN y se importan recién cuando se usan (así boto3 no hace falta si nadie usa `aws`). Un
plugin suma los suyos con un entry point del grupo ENTRY_POINT_GROUP que apunta a la clase.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

ENTRY_POINT_GROUP = "casebook.providers"
KINDS = ("opensearch", "logs", "sql")
log = logging.getLogger("casebook")

# (kind, name) -> "módulo:Clase"
BUILTIN = {
    ("opensearch", "dashboards-proxy"): "casebook.providers.opensearch:DashboardsProxy",
    ("opensearch", "direct"): "casebook.providers.opensearch:Direct",
    ("logs", "aws"): "casebook.providers.cloudwatch:Aws",
    ("sql", "mysql-ssh"): "casebook.providers.mysql:MysqlSsh",
}


class ProviderError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: type = str
    required: bool = False
    default: Any = None
    help: str = ""
    choices: tuple[str, ...] | None = None


def _plugins() -> dict[tuple[str, str], str]:
    out = {}
    try:
        found = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # un paquete roto no impide arrancar
        log.warning("no pude leer los plugins de proveedores: %s", exc)
        return out
    for ep in found:
        try:
            cls = ep.load()
            out[(cls.kind, cls.name)] = ep.value
        except Exception as exc:
            log.warning("el plugin de proveedor %s no cargó: %s", ep.name, exc)
    return out


_registry: dict[tuple[str, str], str] | None = None


def registry() -> dict[tuple[str, str], str]:
    global _registry
    if _registry is None:
        _registry = {**BUILTIN, **_plugins()}
    return _registry


def get(kind: str, name: str) -> type | None:
    """La clase del proveedor, o None si no está instalado (por ejemplo, un plugin que falta)."""
    target = registry().get((kind, name))
    if target is None:
        return None
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def names(kind: str) -> list[str]:
    return sorted(name for k, name in registry() if k == kind)


def validate(cls: type, values: dict[str, Any], where: str) -> dict[str, Any]:
    """Revisa las opciones de una fuente contra `cls.fields`: tipos, obligatorios, valores permitidos y claves
    desconocidas. Devuelve las opciones con los defaults completados."""
    known = {f.key: f for f in cls.fields}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ProviderError(f"{where}: opción desconocida para el proveedor {cls.name}: {', '.join(unknown)}")
    out = {}
    for f in cls.fields:
        if f.key not in values or values[f.key] in (None, ""):
            if f.required:
                raise ProviderError(f"{where}: falta `{f.key}` ({f.label})")
            out[f.key] = f.default
            continue
        value = values[f.key]
        if f.type is bool and not isinstance(value, bool):
            raise ProviderError(f"{where}: `{f.key}` tiene que ser verdadero o falso")
        if f.type is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ProviderError(f"{where}: `{f.key}` tiene que ser un número entero")
        if f.type is float and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ProviderError(f"{where}: `{f.key}` tiene que ser un número")
        if f.type is list and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise ProviderError(f"{where}: `{f.key}` tiene que ser una lista de textos")
        if f.type is str:
            value = str(value)
        if f.choices and value not in f.choices:
            raise ProviderError(f"{where}: `{f.key}` tiene que ser uno de {', '.join(f.choices)}")
        out[f.key] = float(value) if f.type is float else value
    return out
