"""Credenciales. Nunca se escriben a disco ni se devuelven a Claude.

- Cookies del navegador (las fuentes con login en el navegador): en el keyring del sistema (`casebook login`).
- Usuario y contraseña o API key (OpenSearch directo): en el keyring, como JSON (`casebook login <id>`).
- MySQL: de las [credentials] de cada base (un archivo con `export X=...` o variables de entorno). Si ahí no hay,
  se usa lo guardado con `casebook login db` (o `login db-<nombre>`, una entrada del keyring por base).
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import keyring

from .config import DbCredentials
from .systems import MYSQL_KEYRING, System

SERVICE = "casebook"


def normalize_cookie(raw: str) -> str:
    """Acepta el header tal cual se copia de DevTools ("cookie: a=1; b=2" o solo "a=1; b=2")."""
    value = raw.strip()
    if value.lower().startswith("cookie:"):
        value = value[len("cookie:") :].strip()
    return value


def set_cookie(system: System, raw: str) -> None:
    value = normalize_cookie(raw)
    if "=" not in value:
        raise ValueError("eso no parece un header cookie (se esperaba nombre=valor; ...)")
    keyring.set_password(SERVICE, system.keyring, value)


def get_cookie(system: System) -> str | None:
    return keyring.get_password(SERVICE, system.keyring)


def delete_cookie(system: System) -> None:
    try:
        keyring.delete_password(SERVICE, system.keyring)
    except keyring.errors.PasswordDeleteError:
        pass


def set_secret(system: System, values: dict[str, str]) -> None:
    """Usuario y contraseña o API key de un sistema con auth="secret", como JSON en su entrada del keyring."""
    keyring.set_password(SERVICE, system.keyring, json.dumps(values))


def get_secret(system: System) -> dict[str, str] | None:
    raw = keyring.get_password(SERVICE, system.keyring)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_cookie(cookie: str) -> dict[str, str]:
    pairs = (part.split("=", 1) for part in cookie.split(";") if "=" in part)
    return {name.strip(): value.strip() for name, value in pairs}


def merge_cookie(cookie: str, updates: dict[str, str]) -> str:
    merged = {**parse_cookie(cookie), **updates}
    return "; ".join(f"{name}={value}" for name, value in merged.items())


def refresh_from_response(system: System, cookie: str, set_cookies: dict[str, str]) -> str:
    """El backend de OS rota ACCESS-TOKEN, REFRESH-TOKEN, etc. en cada respuesta: se guarda la versión nueva
    para que la sesión siga viva mientras se use, igual que en el navegador."""
    if not set_cookies:
        return cookie
    updated = merge_cookie(cookie, set_cookies)
    if updated != cookie:
        keyring.set_password(SERVICE, system.keyring, updated)
    return updated


def cookie_names(cookie: str) -> list[str]:
    """Solo los nombres, para diagnóstico."""
    return [part.split("=", 1)[0].strip() for part in cookie.split(";") if "=" in part]


def read_env_file(path: Path, names: set[str]) -> dict[str, str]:
    """Lee las líneas `export NOMBRE=valor` de un archivo como texto (no se ejecuta). Solo devuelve `names`."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, raw = line.partition("=")
        if not sep or name not in names:
            continue
        try:
            parts = shlex.split(raw)  # quita comillas como lo haría `source`
            value = parts[0] if len(parts) == 1 else raw
        except ValueError:
            value = raw
        out[name] = value
    return out


def env_file(source: DbCredentials) -> Path:
    return Path(source.path).expanduser()


def describe_source(source: DbCredentials, login: str = "db") -> str:
    """Dónde se buscan las credenciales de la base, para los mensajes (nunca los valores)."""
    if source.kind == "env-file":
        return str(env_file(source))
    if source.kind == "env":
        return f"las variables {source.user_var} y {source.password_var}"
    return f"el keyring (casebook login {login})"


def external_db_login(source: DbCredentials | None = None) -> tuple[str, str] | None:
    """Las credenciales de la fuente configurada (archivo o variables de entorno), sin mirar el keyring."""
    source = source or DbCredentials()
    if source.kind == "env-file":
        values = read_env_file(env_file(source), {source.user_var, source.password_var})
    elif source.kind == "env":
        values = os.environ
    else:
        return None
    user, password = values.get(source.user_var), values.get(source.password_var)
    return (user, password) if user and password else None


# Cada base guarda lo suyo en su entrada del keyring (System.keyring): la DEFAULT en MYSQL_KEYRING (`login db`),
# otra en mysql-<nombre> (`login db-<nombre>`).


def db_login_source(source: DbCredentials | None = None, entry: str = MYSQL_KEYRING,
                    login: str = "db") -> str | None:
    """De dónde salen las credenciales de MySQL (para diagnóstico; nunca los valores)."""
    source = source or DbCredentials()
    if external_db_login(source):
        return describe_source(source, login)
    if keyring.get_password(SERVICE, entry):
        return f"keyring (casebook login {login})"
    return None


def set_db_login(user: str, password: str, entry: str = MYSQL_KEYRING) -> None:
    if not user or not password:
        raise ValueError("usuario y contraseña son obligatorios")
    keyring.set_password(SERVICE, entry, json.dumps({"user": user, "password": password}))


def delete_db_login(entry: str = MYSQL_KEYRING) -> None:
    try:
        keyring.delete_password(SERVICE, entry)
    except keyring.errors.PasswordDeleteError:
        pass


def get_db_login(source: DbCredentials | None = None, entry: str = MYSQL_KEYRING) -> tuple[str, str] | None:
    """Primero las de la fuente configurada (si el equipo cambia la contraseña y la herramienta reescribe el
    archivo, aquí se toma sola); si no hay, las del keyring."""
    found = external_db_login(source)
    if found:
        return found
    raw = keyring.get_password(SERVICE, entry)
    if not raw:
        return None
    data = json.loads(raw)
    return data["user"], data["password"]
