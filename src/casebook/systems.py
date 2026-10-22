"""Registro de los sistemas con sesión: un entorno de OpenSearch por cada uno de config.toml, CloudWatch y MySQL.

Cada sistema tiene el id que se usa en `login <id>`, la entrada del keyring donde se guarda su sesión, el proveedor
que habla con él (providers/) y los textos para mostrarlo. Es la única fuente de esa lista: el login, el native
host, el doctor, el instalador y las tools la leen de aquí.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import providers
from .config import Config


@dataclass(frozen=True)
class System:
    id: str  # el de `login <id>` y el que entrega la extensión
    kind: str  # "opensearch", "cloudwatch" o "mysql"
    keyring: str  # nombre de la entrada en el keyring
    label: str
    description: str = ""
    check: str = ""  # nombre del chequeo en doctor y en el visor
    env: str | None = None  # el entorno, para OpenSearch
    provider: str = ""  # el `provider` de config.toml
    auth: str = "none"  # "browser", "secret", "external", "none" o "missing" (el proveedor no está instalado)
    short: str = ""  # nombre corto para el panel de conexiones del visor

    @property
    def browser(self) -> bool:
        """La sesión sale del navegador (login con Chrome y extensión)."""
        return self.auth == "browser"


# Entrada del keyring de MySQL (`login db`). La de CloudWatch la puede fijar el proveedor (keyring_entry).
MYSQL_KEYRING = "mysql-prod-reader"

# El tipo de sistema -> el `kind` de sus proveedores.
PROVIDER_KIND = {"opensearch": "opensearch", "cloudwatch": "logs", "mysql": "sql"}


def _auth(kind: str, name: str, options: dict[str, Any]) -> str:
    cls = providers.get(PROVIDER_KIND[kind], name)
    if cls is None:
        return "missing"
    return cls.auth_for(options) if hasattr(cls, "auth_for") else cls.auth


def opensearch(cfg: Config, env: str) -> System:
    target = cfg.opensearch[env]
    return System(
        target.system or f"os-{env}",
        "opensearch",
        f"opensearch-{env}",
        target.label or f"OpenSearch {env}",
        target.description,
        f"opensearch:{env}",
        env,
        target.provider,
        _auth("opensearch", target.provider, target.options),
        short=f"os-{env}",
    )


def cloudwatch(cfg: Config) -> System:
    cls = providers.get("logs", cfg.cw_provider)
    description = f"Log Insights ({cls.label})" if cls else "Log Insights"
    keyring_entry = getattr(cls, "keyring_entry", None) or f"cloudwatch-{cfg.cw_provider}"
    return System("cw", "cloudwatch", keyring_entry, "CloudWatch", description, f"cloudwatch ({cfg.cw_provider})", None,
                  cfg.cw_provider, _auth("cloudwatch", cfg.cw_provider, cfg.cw_options), short=cfg.cw_provider)


def mysql(cfg: Config) -> System:
    return System("db", "mysql", MYSQL_KEYRING, "MySQL réplica", "réplica de solo lectura", "mysql (reader)",
                  None, cfg.db.provider, _auth("mysql", cfg.db.provider, {}), short="db")


def all_systems(cfg: Config) -> list[System]:
    """Los sistemas configurados (en config.toml o en el pack), en orden: OpenSearch, CloudWatch, MySQL."""
    out = [opensearch(cfg, env) for env in cfg.opensearch]
    if cfg.cw_provider:
        out.append(cloudwatch(cfg))
    if cfg.db.provider:
        out.append(mysql(cfg))
    return out


def browser_systems(cfg: Config) -> list[System]:
    return [s for s in all_systems(cfg) if s.browser]


def get(cfg: Config, system_id: str) -> System:
    for system in all_systems(cfg):
        if system.id == system_id:
            return system
    raise KeyError(system_id)


def ids(cfg: Config) -> list[str]:
    return [s.id for s in all_systems(cfg)]


def provider(cfg: Config, system: System):
    """La instancia del proveedor de un sistema, o None si no está instalado."""
    cls = providers.get(PROVIDER_KIND[system.kind], system.provider)
    return None if cls is None else cls(cfg, system)


def missing_provider_message(system: System) -> str:
    return (f"el proveedor «{system.provider}» de {system.label} no está instalado (falta su plugin); "
            f"opciones: {', '.join(providers.names(PROVIDER_KIND[system.kind]))}")
