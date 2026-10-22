"""Un proveedor de logs en memoria para probar las tools cw_* sin ningún backend (ni AWS ni un proxy)."""
from casebook import providers
from casebook.backends.errors import BackendError
from casebook.providers.base import Estimate, LogGroup, LogsClient, Provider, QueryState

QUERY_ID = "18f66044-54ce-472c-bda3-a9252023fdee"


class FakeLogs:
    """Lo que devuelve el backend y lo que se le pidió (calls)."""

    def __init__(self):
        self.calls = []
        self.estimate_resp = Estimate(2e9, 0.01, True, 1)
        self.polls = [
            QueryState("RUNNING"),
            QueryState(
                "COMPLETE",
                rows=[
                    {"@timestamp": "2026-10-14 10:00:00.000", "@message": "upload accessToken=ya29.abcdefghijklmnop ok cliente@x.com", "@ptr": "PTR1"},
                    {"@timestamp": "2026-10-14 10:01:00.000", "@message": "otro@gmail.com falló", "@ptr": "PTR2"},
                ],
                records_matched=2, records_scanned=1000, bytes_scanned=3e9, cost_usd=0.015, estimated_cost_usd=0.01,
                duration_ms=900,
            ),
        ]
        self.surrounding_resp = [{"timestamp": 1, "message": "antes Bearer abcdefghijklmnop"}, {"timestamp": 2, "message": "después"}]


current = FakeLogs()


class FakeLogsClient(LogsClient):
    async def log_groups(self, search):
        return [LogGroup("/ecs/acme-api", None, 10)]

    async def estimate(self, log_groups, start, end):
        current.calls.append(("estimate", log_groups, start, end))
        return current.estimate_resp

    async def start_query(self, log_groups, query, start, end):
        current.calls.append(("start", log_groups, query))
        return QUERY_ID

    async def get_query(self, query_id):
        current.calls.append(("get", query_id))
        return current.polls.pop(0) if len(current.polls) > 1 else current.polls[0]

    async def cancel(self, query_id):
        current.calls.append(("cancel", query_id))

    async def surrounding(self, query_id, ptr, events):
        current.calls.append(("surrounding", query_id, ptr, events))
        if isinstance(current.surrounding_resp, Exception):
            raise current.surrounding_resp
        return current.surrounding_resp


class FakeLogsProvider(Provider):
    kind, name, label, auth = "logs", "fake", "Logs de prueba", "none"

    def client(self):
        return FakeLogsClient()


def install(monkeypatch) -> FakeLogs:
    """Registra el proveedor "fake" y devuelve su estado (nuevo en cada test)."""
    global current
    current = FakeLogs()
    monkeypatch.setitem(providers.BUILTIN, ("logs", "fake"), f"{__name__}:FakeLogsProvider")
    monkeypatch.setattr(providers, "_registry", None)
    return current


__all__ = ["BackendError", "FakeLogs", "FakeLogsClient", "install", "QUERY_ID"]
