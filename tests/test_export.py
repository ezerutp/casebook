import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from casebook import cli, export
from casebook.config import Config
from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store
from casebook.viewer.app import create_app
from demo_pack import acme


@pytest.fixture
def session(tmp_path):
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    rec.start_session("SUP-13510", card="SUP-13510")
    rec.note("Hipótesis: el upload a Google falló por el gclid.")
    with rec.query("os_search", {"index": "leads-*"}, 'GET leads-*/_search\n{"size": 1}') as q:
        q.finish(
            ["_id", "msg"],
            [[str(i), f"linea {i} | con pipe\ny salto"] for i in range(30)],
            {"env": "prod", "total_hits": 30},
            tables=[{"name": "aggs.por_mes", "columns": ["key", "doc_count"], "rows": [["2026-10", 30]]}],
        )
    with rec.query("cw_estimate", {"log_group": "X"}, None) as q:
        q.finish(["bytes"], [[1]], {"estimated_cost_usd": 0.5})
    with rec.query("cw_query", {"log_group": "X"}, "fields @message | limit 1") as q:
        q.finish(["@message"], [["ok"]], {"cost_usd": 0.0021})
    with pytest.raises(RuntimeError):
        with rec.query("db_select", {}, "SELECT 1 LIMIT 100"):
            raise RuntimeError("túnel caído")
    rec.draft("SUP-13510", "El upload salió bien; Google rechazó la conversión por gclid vencido.")
    rec.note("Conclusión: problema del lado de Google.")
    yield store, rec
    store.close()


def test_render_markdown(session):
    store, rec = session
    md = export.render(store, rec.session_id, max_rows=5)

    assert md.startswith("# SUP-13510\n")
    assert "4 consultas (1 con error) · CloudWatch $0.0021" in md  # el estimado no cuenta como gasto
    assert "estimado $0.5000" in md
    assert "## Conclusión (última nota)\n\nConclusión: problema del lado de Google." in md
    assert "**Nota · " in md and "Hipótesis" in md
    assert "`os_search` · ok · 30 filas" in md and "30 hits totales" in md
    assert "| linea 0 \\| con pipe y salto |" in md  # pipes escapados, sin saltos de línea
    assert "_25 filas más en el visor._" in md
    assert "**aggs.por_mes**" in md and "| 2026-10 | 30 |" in md
    assert "```sql\nSELECT 1 LIMIT 100\n```" in md and "> Error: RuntimeError: túnel caído" in md
    assert "## Borradores de comentarios para Jira\n\n### SUP-13510" in md
    # el orden del timeline respeta el orden de los eventos
    assert md.index("Hipótesis") < md.index("`os_search`") < md.index("`cw_query`") < md.index("`db_select`")


def test_write_default_path(session, tmp_path, monkeypatch):
    store, rec = session
    monkeypatch.setattr(export, "EXPORT_DIR", tmp_path / "exports")
    path = export.write(store, rec.session_id)
    assert path == tmp_path / "exports" / f"SUP-13510-{rec.session_id}.md"
    assert path.read_text().startswith("# SUP-13510")


def test_viewer_export_endpoint(session):
    store, rec = session
    client = TestClient(create_app(store))
    resp = client.get(f"/api/sessions/{rec.session_id}/export.md?download=true&rows=2")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/markdown")
    assert f'filename="SUP-13510-{rec.session_id}.md"' in resp.headers["content-disposition"]
    assert "_28 filas más en el visor._" in resp.text
    assert client.get("/api/sessions/999/export.md").status_code == 404


def test_cli_export_by_card(session, tmp_path, monkeypatch, capsys):
    store, rec = session
    cfg = acme()
    cfg.db_path = store.path
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    assert cli.main(["export", "SUP-13510", "-o", "-"]) == 0
    assert capsys.readouterr().out.startswith("# SUP-13510")
    out_file = tmp_path / "x.md"
    assert cli.main(["export", str(rec.session_id), "-o", str(out_file)]) == 0
    assert out_file.read_text().startswith("# SUP-13510")
    assert cli.main(["export", "NO-EXISTE"]) == 1


def test_mcp_draft_and_export(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_DIR", tmp_path / "exports")
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    mcp = build(rec, "http://v", acme())

    async def go():
        with pytest.raises(ToolError, match="no hay sesión"):
            await mcp.call_tool("session_export", {})
        await mcp.call_tool("session_start", {"name": "SUP-1"})
        r = await mcp.call_tool("jira_comment_draft", {"card": "sup-1", "text": "  hallazgo  "})
        assert "no se publicó" in r.content[0].text
        with pytest.raises(ToolError, match="card inválida"):
            await mcp.call_tool("jira_comment_draft", {"card": "nada", "text": "x"})
        r = await mcp.call_tool("session_export", {})
        return r.content[0].text

    text = anyio.run(go)
    draft = store.events(rec.session_id)[-1]
    assert draft["type"] == "draft" and draft["payload"] == {"card": "SUP-1", "text": "hallazgo"}
    assert str(tmp_path / "exports") in text
    assert "### SUP-1" in (tmp_path / "exports" / f"SUP-1-{rec.session_id}.md").read_text()


def test_draft_versions_preserved_and_exported_by_card(tmp_path):
    store = Store(tmp_path / "versions.db")
    rec = Recorder(store)
    first = rec.start_session("SUP-1")
    rec.draft("SUP-1", "Original")
    rec.draft("SUP-2", "Otro ticket")
    rec.draft("SUP-1", "Revisión")
    rec.resume_session(first["id"])
    rec.draft("SUP-1", "Actual")
    md = export.render(store, first["id"])
    assert md.count("### SUP-1 ·") == 1
    assert "3 versiones (actual: 3)" in md
    assert md.index("Actual") < md.index("Revisión") < md.index("Original")
    assert "### SUP-2 ·" in md
    assert len([e for e in store.events(first["id"]) if e["type"] == "draft"]) == 4
    second = rec.start_session("Otra sesión")
    rec.draft("SUP-1", "Independiente")
    assert "Historial" not in export.render(store, second["id"])
    store.close()
