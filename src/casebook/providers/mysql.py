"""Proveedor de SQL: MySQL (réplica de solo lectura) por un túnel SSH (`ssh -N -L` a un host de ~/.ssh/config).

Las capas de solo lectura están en backends/mysql.py. Las credenciales salen de [db.credentials] (archivo con
`export X=...` o variables de entorno) y, si ahí no hay, del keyring (`casebook login db`).
"""

from __future__ import annotations

from ..backends.mysql import MySQLClient
from . import Field
from .base import Provider


class MysqlSsh(Provider):
    kind = "sql"
    name = "mysql-ssh"
    label = "MySQL por túnel SSH (réplica de solo lectura)"
    auth = "external"
    fields = (
        Field("ssh_host", "Host de ~/.ssh/config", required=True, help="el servidor SSH por el que se abre el túnel"),
        Field("host", "Endpoint de la base", required=True, help="el reader del cluster, nunca el writer"),
        Field("port", "Puerto", int, default=3306),
        Field("database", "Base", required=True),
        Field("reader_host_pattern", "Texto que tiene que estar en el host", default="",
              help="una guarda más contra el writer; en Aurora, \"cluster-ro\""),
    )

    def login_hint(self) -> str:
        return self.cfg.db.credentials.hint

    def client(self) -> MySQLClient:
        return MySQLClient(self.cfg.db)

    def doctor_checks(self, heal: bool = True):
        from ..doctor import check_db

        return check_db(self.cfg)
