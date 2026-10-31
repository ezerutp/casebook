"""Registro de los sistemas con sesión: uno por cada fuente de config.toml ([sources.<tipo>.<nombre>]).

Cada sistema tiene el id que se usa en `login <id>`, la entrada del keyring donde se guarda su sesión, el proveedor
que habla con él (providers/) y los textos para mostrarlo. Es la única fuente de esa lista: el login, el native
host, el doctor, el instalador y las tools la leen de aquí.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config, providers
from .config import Config


@dataclass(frozen=True)
class System:
    id: str  # el de `login <id>` y el que entrega la extensión
    kind: str  # "opensearch", "cloudwatch" o "mysql"
    keyring: str  # nombre de la entrada en el keyring
    label: str
    description: str = ""
    check: str = ""  # nombre del chequeo en doctor y en el visor
    env: str | None = None  # el nombre de la instancia ([sources.<tipo>.<env>]); el `env` de las tools
    provider: str = ""  # el `provider` de config.toml
    auth: str = "none"  # "browser", "secret", "external", "none" o "missing" (el proveedor no está instalado)
    short: str = ""  # nombre corto para el panel de conexiones del visor

    @property
    def browser(self) -> bool:
        """La sesión sale del navegador (login con Chrome y extensión)."""
        return self.auth == "browser"


# Entrada del keyring de la base DEFAULT (`login db`); otra base usa mysql-<nombre>. La de CloudWatch la puede
# fijar el proveedor (keyring_entry).
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
        config.system_id("opensearch", env, target.system),
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


def _single(cfg: Config, kind: str) -> str:
    """El nombre de la única instancia de un tipo (para llamadas que no dicen cuál)."""
    names = list(getattr(cfg, kind))
    if len(names) != 1:
        raise KeyError(f"{kind}: hay {len(names)} fuentes ({', '.join(names) or 'ninguna'}); di cuál")
    return names[0]


def cloudwatch(cfg: Config, env: str | None = None) -> System:
    env = env or _single(cfg, "cloudwatch")
    src = cfg.cloudwatch[env]
    default = env == config.DEFAULT
    name = config.env_name("cloudwatch", env, src)
    cls = providers.get("logs", src.provider)
    description = src.description or (f"Log Insights ({cls.label})" if cls else "Log Insights")
    keyring_entry = getattr(cls, "keyring_entry", None) or f"cloudwatch-{src.provider}"
    return System(
        config.system_id("cloudwatch", env, src.system),
        "cloudwatch",
        keyring_entry if default else f"{keyring_entry}-{env}",
        # Con el nombre que ven Claude y el visor: «CloudWatch prod» si la default tiene environment.
        src.label or ("CloudWatch" if name == config.DEFAULT else f"CloudWatch {name}"),
        description,
        f"cloudwatch ({src.provider})" if default else f"cloudwatch:{env} ({src.provider})",
        env,
        src.provider,
        _auth("cloudwatch", src.provider, src.options),
        short=src.provider if default else f"cw-{env}",
    )


def mysql(cfg: Config, env: str | None = None) -> System:
    env = env or _single(cfg, "mysql")
    src = cfg.mysql[env]
    default = env == config.DEFAULT
    name = config.env_name("mysql", env, src)
    return System(
        config.system_id("mysql", env, src.system),
        "mysql",
        MYSQL_KEYRING if default else f"mysql-{env}",
        src.label or ("MySQL réplica" if name == config.DEFAULT else f"MySQL {name}"),
        src.description or "réplica de solo lectura",
        "mysql (reader)" if default else f"mysql:{env}",
        env,
        src.provider,
        _auth("mysql", src.provider, src.options),
        short="db" if default else f"db-{env}",
    )


def all_systems(cfg: Config) -> list[System]:
    """Los sistemas configurados (en config.toml o en el pack), en orden: OpenSearch, CloudWatch, MySQL."""
    return [*(opensearch(cfg, env) for env in cfg.opensearch), *(cloudwatch(cfg, env) for env in cfg.cloudwatch),
            *(mysql(cfg, env) for env in cfg.mysql)]


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
