"""Un proveedor de logs de prueba que inicia sesión en el navegador (auth="browser"), para probar el login, la
extensión y el native host con una fuente que no es OpenSearch. Valida la cookie contra {api}/me."""
import httpx

from casebook import providers
from casebook.login import BrowserTarget, Validation
from casebook.providers import Field
from casebook.providers.base import Provider
from casebook.config import CloudWatchSource

API, ORIGIN = "https://api-logs.acme.example", "https://logs.acme.example"
MESSAGES = {"NO_SESSION": "el navegador todavía no tiene una sesión", "EXPIRED": "esa sesión ya venció"}


def validate(cookie: str) -> Validation:
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{API}/me", headers={"cookie": cookie})
    except httpx.HTTPError as exc:
        return Validation(False, f"no pude conectar ({exc})", retry=False)
    if resp.status_code == 200:
        return Validation(True, "la acepta", info=(f"Sesión de {resp.json().get('fullName', '?')}",))
    code = resp.json().get("code") if resp.headers.get("content-type", "").startswith("application/json") else None
    return Validation(False, MESSAGES.get(code, f"la rechazó (HTTP {resp.status_code})"))


class WebLogsProvider(Provider):
    kind, name, label, auth = "logs", "web-logs", "Logs con login web (prueba)", "browser"
    fields = (Field("api", "API", required=True), Field("origin", "Web", required=True))

    def login_target(self):
        return BrowserTarget(f"{ORIGIN}/login", f"{API}/me", validate, cookie_domains=("acme.example",))

    def prepare_login(self):
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{API}/health")
        except httpx.HTTPError as exc:
            return f"no llego a {API} ({exc})"
        return f"responde con error (HTTP {resp.status_code})" if resp.status_code >= 500 else None


def install(monkeypatch, cfg):
    """Registra el proveedor y pone CloudWatch de `cfg` a usarlo. Devuelve cfg."""
    monkeypatch.setitem(providers.BUILTIN, ("logs", "web-logs"), f"{__name__}:WebLogsProvider")
    monkeypatch.setattr(providers, "_registry", None)
    cfg.cloudwatch = {"default": CloudWatchSource("web-logs", {"api": API, "origin": ORIGIN})}
    return cfg


def handler(good="osid=ok", reachable=True):
    """El backend simulado: /health sin sesión y /me con la cookie buena."""

    def respond(request):
        path = request.url.path
        if path == "/health":
            return httpx.Response(200 if reachable else 503)
        cookie = request.headers.get("cookie")
        if cookie != good:
            code = "NO_SESSION" if "tracker" in (cookie or "") else "EXPIRED"
            return httpx.Response(401, json={"code": code})
        if path == "/me":
            return httpx.Response(200, json={"fullName": "Ana Dev"})
        return httpx.Response(404)

    return respond
