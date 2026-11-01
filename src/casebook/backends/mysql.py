"""MySQL por un túnel SSH contra la réplica de solo lectura, con varias capas de solo lectura:

1. Si la fuente define `reader_host_pattern` (en Aurora, `cluster-ro`), el host tiene que contenerlo.
2. Al conectar se verifica `@@innodb_read_only = 1` o `@@super_read_only = 1` (la instancia no acepta escrituras);
   si no, se corta.
3. La sesión fija `MAX_EXECUTION_TIME`, y cada consulta corre dentro de `START TRANSACTION READ ONLY`. (En
   Aurora 3 el reader ignora `SET SESSION TRANSACTION READ ONLY`, por eso la transacción es explícita.)
4. El SQL ya llega validado por guards.check_sql (una sentencia, solo lectura, LIMIT forzado).
5. Antes de cada SELECT se corre EXPLAIN y se rechazan los planes demasiado caros.
"""

from __future__ import annotations

import datetime as dt
import decimal
import threading
from collections import defaultdict
from typing import Any

import pymysql

from .. import credentials
from ..config import Database, MySqlSource
from .errors import BackendError, SessionExpired
from .tunnel import SshTunnel

ER_ACCESS_DENIED = 1045
ER_QUERY_TIMEOUT = 3024
JS_SAFE_INT = 2**53


class PlanTooExpensive(BackendError):
    def __init__(self, message: str):
        super().__init__(message)
        self.plan: dict[str, Any] | None = None


def to_json(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, int) and abs(value) >= JS_SAFE_INT:
        return str(value)  # el visor es JS: un BIGINT grande perdería precisión
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + value.hex()
    return value


def check_plan(columns: list[str], rows: list[tuple], db: Database) -> None:
    plan = [dict(zip(columns, r)) for r in rows]
    for step in plan:
        if (step.get("type") or "").upper() == "ALL" and int(step.get("rows") or 0) > db.full_scan_max_rows:
            raise PlanTooExpensive(
                f"el plan hace un full scan de `{step.get('table')}` (~{int(step['rows']):,} filas estimadas). "
                "Filtra por una columna indexada (mira db_describe) o acota la consulta."
            )
    per_select: dict[Any, int] = defaultdict(lambda: 1)
    for step in plan:
        per_select[step.get("id")] *= max(int(step.get("rows") or 1), 1)
    worst = max(per_select.values(), default=0)
    if worst > db.plan_max_rows:
        raise PlanTooExpensive(
            f"el plan estima examinar ~{worst:,} filas (máximo {db.plan_max_rows:,}). "
            "Usa filtros por columnas indexadas o divide la consulta."
        )


class MySQLClient:
    """Una base (`source`) con las guardas de [db] (`guards`, compartidas: se leen en cada consulta, así un cambio
    del visor se aplica sin reconectar). `keyring_entry` y `login` son los de su sistema (`login db-<nombre>`)."""

    def __init__(self, source: MySqlSource, guards: Database | None = None,
                 keyring_entry: str = credentials.MYSQL_KEYRING, login: str = "db"):
        if not source.host:
            raise BackendError("no hay base configurada ([sources.mysql.<nombre>] en config.toml o en el pack)")
        if source.reader_host_pattern and source.reader_host_pattern not in source.host:
            raise BackendError(f"el host {source.host} no es un endpoint reader ({source.reader_host_pattern}); "
                               "no se conecta")
        self.source = source
        self.db = guards or Database()
        self.keyring_entry, self.login = keyring_entry, login
        self.tunnel = SshTunnel(source.ssh_host, source.host, source.port)
        self._conn: pymysql.connections.Connection | None = None
        self._lock = threading.Lock()  # una consulta a la vez

    # --- conexión ---------------------------------------------------------------------------------------------

    def _connect(self) -> pymysql.connections.Connection:
        source = self.source.credentials
        login = credentials.get_db_login(source, self.keyring_entry)
        if not login:
            raise SessionExpired(
                f"no hay credenciales de la base en {credentials.describe_source(source, self.login)}. Pídele al "
                f"usuario que {source.hint} (o `casebook login {self.login}` para guardarlas en el keyring)"
            )
        user, password = login
        port = self.tunnel.ensure()
        try:
            conn = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user=user,
                password=password,
                database=self.source.database,
                charset="utf8mb4",
                autocommit=True,
                connect_timeout=10,
                read_timeout=self.db.max_execution_ms / 1000 + 15,
            )
        except pymysql.err.OperationalError as exc:
            if exc.args and exc.args[0] == ER_ACCESS_DENIED:
                raise SessionExpired(
                    f"la base rechazó las credenciales de "
                    f"{credentials.db_login_source(source, self.keyring_entry, self.login)}. Pídele al usuario "
                    f"que las actualice: {source.hint}"
                ) from exc
            raise BackendError(f"no se pudo conectar a la base: {exc}") from exc
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT @@innodb_read_only, @@super_read_only")
                innodb_ro, super_ro = cur.fetchone()
                if int(innodb_ro) != 1 and int(super_ro) != 1:
                    raise BackendError("la instancia conectada acepta escrituras (no es el reader); se aborta")
                cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(self.db.max_execution_ms)}")
        except Exception:
            conn.close()
            raise
        return conn

    def _connection(self) -> pymysql.connections.Connection:
        if self._conn is None or not self._conn.open:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self.tunnel.close()

    # --- consultas --------------------------------------------------------------------------------------------

    def _execute(self, sql: str) -> tuple[list[str], list[list[Any]]]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(sql)
                columns = [d[0] for d in cur.description or []]
                rows = [[to_json(v) for v in row] for row in cur.fetchall()]
            return columns, rows
        except pymysql.err.OperationalError as exc:
            code = exc.args[0] if exc.args else None
            if code == ER_QUERY_TIMEOUT:
                raise BackendError(
                    f"la consulta superó {self.db.max_execution_ms / 1000:g} s y la base la cortó; acótala"
                ) from exc
            # Conexión caída u otro error de red: se descarta para reconectar en la próxima.
            self._conn = None
            raise BackendError(f"error de la base: {exc.args[-1] if exc.args else exc}") from exc
        except pymysql.err.MySQLError as exc:
            raise BackendError(f"error de la base: {exc.args[-1] if exc.args else exc}") from exc

    def query(self, sql: str, kind: str, allow_expensive: bool = False) -> dict[str, Any]:
        """Sincrónico: se llama desde un thread. Devuelve columnas, filas y, para SELECT, el plan.
        `allow_expensive` salta el chequeo del plan (solo después de una aprobación explícita en el visor);
        MAX_EXECUTION_TIME sigue aplicando."""
        with self._lock:
            plan = None
            if kind == "select":
                plan_columns, plan_rows = self._execute(f"EXPLAIN {sql}")
                plan = {"columns": plan_columns, "rows": plan_rows}
                if not allow_expensive:
                    try:
                        check_plan(plan_columns, [tuple(r) for r in plan_rows], self.db)
                    except PlanTooExpensive as exc:
                        exc.plan = plan
                        raise
            self._execute("START TRANSACTION READ ONLY")
            try:
                columns, rows = self._execute(sql)
            finally:
                if self._conn is not None:
                    self._execute("ROLLBACK")
            return {"columns": columns, "rows": rows, "plan": plan}

    def server_info(self) -> dict[str, Any]:
        with self._lock:
            columns, rows = self._execute("SELECT VERSION() AS version, @@innodb_read_only AS innodb_read_only, @@hostname AS host")
            return dict(zip(columns, rows[0]))
