"""Textos para Claude: salen del pack (dominio, ejemplos de card, pistas de cada fuente y triage de Slack)."""
import anyio
from fastapi.testclient import TestClient

from casebook import config, server, slack_watch
from casebook.events import Recorder
from casebook.store import Store
from casebook.viewer.app import create_app
from demo_pack import acme, no_pack


def tools(cfg, tmp_path):
    store = Store(tmp_path / "t.db")
    listed = {t.name: t for t in anyio.run(server.build(Recorder(store), None, cfg).list_tools)}
    store.close()
    return listed


def everything(cfg, tmp_path) -> str:
    return server.instructions(cfg) + "\n".join(t.description for t in tools(cfg, tmp_path).values())


def test_without_pack_texts_are_generic(tmp_path):
    no_pack()
    cfg = config.load(tmp_path / "config.toml")
    text = everything(cfg, tmp_path)
    for word in ("Acme", "SUP", "acme", "orders-", "db-login"):
        assert word not in text
    assert "investigar casos de soporte." in text and 'name="CASO-1234"' in text
    assert "OpenSearch:" not in text and "CloudWatch cuesta" not in text  # sin esas fuentes, sin sus reglas


def test_with_demo_pack_texts_name_the_team(tmp_path):
    cfg = acme()
    instructions = server.instructions(cfg)
    assert "casos de soporte de Acme" in instructions and 'card="SUP-1200"' in instructions
    assert "«SUP-1210 · Tema»" in instructions and "CloudWatch cuesta" in instructions
    listed = tools(cfg, tmp_path)
    assert "orders-pending-*" in listed["os_indices"].description
    assert '"prod-archive" (orders-* en prod-archive)' in listed["os_search"].description
    assert "/ecs/acme-api" in listed["cw_log_groups"].description
    assert "base `acme`" in listed["db_select"].description
    assert 'name="SUP-1200"' in listed["session_start"].description


def test_slack_triage_defaults_are_generic():
    job = slack_watch.Job(ts="1.1", thread_ts="1.1", user_id="U1", user_name="ana", preview="hola")
    prompt = slack_watch.analysis_prompt(job, "C1", 7)
    assert "skill" not in prompt.split("3. ")[1].split("\n")[0] and "SUP" not in prompt
    assert "escalate: Escalar con una card" in prompt and "card solo si outcome=escalate" in prompt
    schema = slack_watch.analysis_schema(config.Triage())
    assert schema["properties"]["outcome"]["enum"] == ["explain", "escalate", "need_info", "not_issue"]


def test_slack_triage_from_demo_pack():
    triage = acme().slack_triage
    job = slack_watch.Job(ts="1.1", thread_ts="1.1", user_id="U1", user_name="ana", preview="hola")
    prompt = slack_watch.analysis_prompt(job, "C1", 7, triage=triage)
    assert "Carga la skill acme-triage" in prompt and "ticket: Abrir ticket" in prompt
    assert 'en inglés, empezando con "Hey @nombre"' in prompt
    text = slack_watch.format_analysis(job, {"outcome": "ticket", "summary": "s", "card": "Título"}, triage)
    assert text.startswith("**Abrir ticket**") and "**Ticket:**\nTítulo" in text


def test_invalid_triage_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[slack.triage]\nescalation = "nada"\n')
    try:
        config.load(path)
    except config.SettingError as exc:
        assert "escalation" in str(exc)
    else:
        raise AssertionError("tenía que fallar")


def test_meta_carries_card_texts(tmp_path):
    meta = TestClient(create_app(Store(tmp_path / "s.db"), acme())).get("/api/meta").json()
    assert (meta["pack"], meta["card_prefix"], meta["card_example"]) == ("Acme", "SUP", "SUP-1200")
