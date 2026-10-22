"""Fuentes configuradas a mano: el catálogo de proveedores (con sus campos) y agregar, probar y quitar fuentes de
config.toml. Lo usan `casebook source` y Ajustes → Fuentes en el visor; los formularios salen de los
`fields` de cada proveedor, no hay uno escrito para cada uno.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from . import config, providers, systems
from .config import Config

# El tipo de fuente (como en [sources.<tipo>]) -> el `kind` de sus proveedores.
PROVIDER_KIND = systems.PROVIDER_KIND
KIND_LABELS = {"opensearch": "OpenSearch", "cloudwatch": "CloudWatch Logs Insights", "mysql": "MySQL"}
# Lo que se puede poner en cualquier fuente además de las opciones del proveedor.
META_FIELDS = (
    providers.Field("label", "Nombre para mostrar"),
    providers.Field("description", "Descripción corta", help="qué hay en la fuente, para el instalador y el visor"),
    providers.Field("hint", "Pista para Claude", help="familias de índices, log groups o tablas"),
)


def _field(f: providers.Field) -> dict[str, Any]:
    return {"key": f.key, "label": f.label, "type": f.type.__name__, "required": f.required, "default": f.default,
            "help": f.help, "choices": list(f.choices) if f.choices else None}


def catalog() -> dict[str, Any]:
    """Por tipo de fuente: sus proveedores instalados con sus campos."""
    out = {}
    for kind, provider_kind in PROVIDER_KIND.items():
        items = []
        for name in providers.names(provider_kind):
            cls = providers.get(provider_kind, name)
            items.append({"name": name, "label": cls.label or name, "auth": getattr(cls, "auth", "none"),
                          "fields": [_field(f) for f in cls.fields]})
        out[kind] = {"label": KIND_LABELS[kind], "providers": items, "meta": [_field(f) for f in META_FIELDS]}
    return out


def parse(field: providers.Field, raw: Any) -> Any:
    """Un valor escrito por una persona (texto) al tipo del campo. Vacío = sin valor."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if field.type is bool:
        if text.lower() in ("true", "sí", "si", "s", "y", "yes", "1"):
            return True
        if text.lower() in ("false", "no", "n", "0"):
            return False
        raise config.SettingError(f"{field.key}: tiene que ser sí o no")
    if field.type is int:
        try:
            return int(text)
        except ValueError:
            raise config.SettingError(f"{field.key}: tiene que ser un número entero") from None
    if field.type is float:
        try:
            return float(text)
        except ValueError:
            raise config.SettingError(f"{field.key}: tiene que ser un número") from None
    if field.type is list:
        try:
            value = tomllib.loads(f"v = {text}")["v"] if text.startswith("[") else [p.strip() for p in text.split(",")]
        except tomllib.TOMLDecodeError:
            raise config.SettingError(f"{field.key}: una lista separada por comas") from None
        return [str(v) for v in value if str(v)]
    return text


def build_values(kind: str, provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Los valores de una fuente nueva, con tipos, validados contra el proveedor (sin escribir nada)."""
    cls = providers.get(PROVIDER_KIND[kind], provider)
    if cls is None:
        raise config.SettingError(f"no hay un proveedor {provider!r} para {kind}; opciones: "
                                  f"{', '.join(providers.names(PROVIDER_KIND[kind]))}")
    known = {f.key: f for f in (*cls.fields, *META_FIELDS)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise config.SettingError(f"opción desconocida para {provider}: {', '.join(unknown)}")
    values = {"provider": provider}
    for key, value in raw.items():
        parsed = parse(known[key], value)
        if parsed is not None:
            values[key] = parsed
    options = {k: v for k, v in values.items() if k in {f.key for f in cls.fields}}
    try:
        providers.validate(cls, options, f"sources.{kind}")
    except providers.ProviderError as exc:
        raise config.SettingError(str(exc)) from None
    return values


def add(kind: str, env: str | None, provider: str, raw: dict[str, Any], path: Path | None = None,
        pack: Any = config._ACTIVE) -> systems.System:
    if kind == "opensearch" and not (env and env.replace("-", "").replace("_", "").isalnum()):
        raise config.SettingError("el entorno tiene que ser letras, números, - o _ (por ejemplo staging)")
    values = build_values(kind, provider, raw)
    path = path or config.CONFIG_PATH
    config.set_source(kind, env if kind == "opensearch" else None, values, path, pack)
    cfg = config.load(path, pack)
    return systems.opensearch(cfg, env) if kind == "opensearch" else (
        systems.cloudwatch(cfg) if kind == "cloudwatch" else systems.mysql(cfg))


def locate(cfg: Config, system_id: str) -> tuple[str, str | None]:
    """(tipo, entorno) de un sistema configurado."""
    system = systems.get(cfg, system_id)
    return {"opensearch": "opensearch", "cloudwatch": "cloudwatch", "mysql": "mysql"}[system.kind], system.env


def origin(cfg: Config, system: systems.System) -> str:
    """"pack", "config" o "pack+config" (el pack la define y config.toml la pisa)."""
    path = cfg.source or config.CONFIG_PATH
    user = config.normalize(config._read(path)).get("sources", {})
    pack_data = config.normalize(config._read(cfg.pack.path)).get("sources", {}) if cfg.pack else {}

    def has(data: dict[str, Any]) -> bool:
        return system.env in data.get("opensearch", {}) if system.kind == "opensearch" else system.kind in data

    found = [name for name, data in (("pack", pack_data), ("config", user)) if has(data)]
    return "+".join(found) or "config"


def listing(cfg: Config) -> list[dict[str, Any]]:
    out = []
    for s in systems.all_systems(cfg):
        out.append({"id": s.id, "kind": s.kind, "env": s.env, "label": s.label, "provider": s.provider,
                    "auth": s.auth, "check": s.check, "origin": origin(cfg, s)})
    return out


def test(cfg: Config, system_id: str) -> list[Any]:
    from . import doctor

    return doctor.check_system(cfg, systems.get(cfg, system_id), heal=False)


def remove(cfg: Config, system_id: str) -> str:
    """Quita la fuente de config.toml. Si viene del pack no se puede quitar desde aquí (se dice)."""
    kind, env = locate(cfg, system_id)
    where = origin(cfg, systems.get(cfg, system_id))
    removed = config.remove_source(kind, env, cfg.source or config.CONFIG_PATH)
    if where == "pack":
        raise config.SettingError(f"{system_id} viene del pack {cfg.pack.name}: se cambia en el pack, no en config.toml")
    if where == "pack+config":
        return "quitados tus cambios; queda la del pack"
    return "quitada" if removed else "no estaba en config.toml"
