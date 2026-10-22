"""Base de los proveedores y los tipos que comparten (resultados de logs normalizados)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from . import Field

if TYPE_CHECKING:
    from ..config import Config
    from ..doctor import Check
    from ..login import BrowserTarget
    from ..systems import System


class Provider:
    kind: ClassVar[str]
    name: ClassVar[str]
    label: ClassVar[str] = ""
    auth: ClassVar[str] = "none"  # "browser", "secret", "external" o "none" (ver providers/__init__.py)
    fields: ClassVar[tuple[Field, ...]] = ()

    def __init__(self, cfg: Config, system: System):
        self.cfg = cfg
        self.system = system

    def login_target(self) -> BrowserTarget | None:
        """auth="browser": dónde iniciar sesión y cómo validar la cookie."""
        return None

    def prepare_login(self) -> str | None:
        """Antes de iniciar sesión (levantar un túnel, ver si hay red). Devuelve el error, o None si se puede."""
        return None

    def secret_fields(self) -> tuple[Field, ...]:
        """auth="secret": lo que `login <id>` le pide al usuario y guarda en el keyring."""
        return ()

    def login_hint(self) -> str:
        """auth="external": cómo se consigue la sesión fuera de este paquete."""
        return ""

    def doctor_checks(self, heal: bool = True) -> list[Check]:
        return []


# --- logs -----------------------------------------------------------------------------------------------------

RUNNING = "RUNNING"
COMPLETE = "COMPLETE"


@dataclass
class LogGroup:
    name: str
    label: str | None = None
    stored_bytes: int | None = None


@dataclass
class Estimate:
    bytes: float
    cost_usd: float
    within_limit: bool = True  # el límite propio del proveedor, si tiene uno por consulta
    limit_usd: float | None = None


@dataclass
class QueryState:
    status: str  # RUNNING, COMPLETE u otro estado final (FAILED, CANCELLED, TIMEOUT...)
    rows: list[dict[str, Any]] = field(default_factory=list)
    records_matched: float | None = None
    records_scanned: float | None = None
    bytes_scanned: float | None = None
    cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    duration_ms: float | None = None
    error: str | None = None
    expired: bool = False


class LogsClient:
    """Lo que necesitan las tools cw_*. Todos los métodos son async."""

    async def log_groups(self, search: str | None) -> list[LogGroup]:
        raise NotImplementedError

    async def estimate(self, log_groups: list[str], start: str, end: str) -> Estimate:
        raise NotImplementedError

    async def start_query(self, log_groups: list[str], query: str, start: str, end: str) -> str:
        """Devuelve el id de la consulta (el `external_id` de las tools)."""
        raise NotImplementedError

    async def get_query(self, query_id: str) -> QueryState:
        raise NotImplementedError

    async def cancel(self, query_id: str) -> None:
        raise NotImplementedError

    async def surrounding(self, query_id: str, ptr: str, events: int) -> list[dict[str, Any]]:
        raise NotImplementedError
