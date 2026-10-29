"""Textos para Claude: salen del pack (dominio, ejemplos de card, pistas de cada fuente y triage de Slack)."""
import anyio
import pytest
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


def test_analysis_investigates_instead_of_a_cheap_triage():
    triage = config.Triage(skill="acme-triage", investigation_skill="acme-investigation", max_queries=5,
                           reply_style="en inglés")
    job = slack_watch.Job(ts="1.1", thread_ts="1.1", user_id="U1", user_name="Ana", preview="hola")
    prompt = slack_watch.analysis_prompt(job, "C1", 7, triage=triage)
    assert "no una investigación completa" not in prompt
    assert "Carga la skill acme-investigation" in prompt and "hasta 5 consultas" in prompt
    assert "no llames session_start" in prompt and "similar_cards" in prompt
    # La respuesta sugerida es para pegar en el hilo: corta y coherente con lo que escribió quien preguntó.
    assert "máximo 4 frases" in prompt and "para contestarle a Ana" in prompt
    assert "no le pidas lo que ya dio" in prompt and "detalles de programador" in prompt
    # Sin skill de investigación (otro equipo) no se menciona ninguna.
    generic = slack_watch.analysis_prompt(job, "C1", 7)
    assert "acme-investigation" not in generic and "no llames session_start" not in generic


def test_analysis_schema_asks_for_evidence_and_confidence():
    schema = slack_watch.analysis_schema(config.Triage())
    assert {"evidence", "confidence"} <= set(schema["required"])
    assert schema["properties"]["confidence"]["enum"] == ["high", "medium", "low"]


def test_format_analysis_lists_evidence_discarded_and_similar_cards():
    job = slack_watch.Job(ts="1.1", thread_ts="1.1", user_id="U1", user_name="Ana", preview="hola")
    text = slack_watch.format_analysis(job, {
        "outcome": "escalate", "summary": "s", "hypothesis": "h", "confidence": "medium",
        "evidence": [{"finding": "No hay orden en los logs", "query_id": "0123456789ab"}, {"finding": "Sin query"}],
        "discarded": ["Regla creada después"], "similar_cards": ["SUP-1 · Venta faltante"]})
    assert "**Confianza:** media" in text and "- No hay orden en los logs (q 0123456789ab)" in text
    assert "- Sin query" in text and "**Descartado:**\n- Regla creada después" in text
    assert "**Cards similares:**\n- SUP-1 · Venta faltante" in text


def test_triage_extra_tools_must_be_exact_mcp_names(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[slack.triage]\nallowed_tools = ["mcp__claude_ai_X__docs"]\n')
    assert config.load(path).slack_triage.allowed_tools == ["mcp__claude_ai_X__docs"]
    for bad in ('["Bash"]', '["mcp__x__*"]', '"mcp__x__docs"'):
        path.write_text(f"[slack.triage]\nallowed_tools = {bad}\n")
        with pytest.raises(config.SettingError, match="allowed_tools"):
            config.load(path)


def test_pack_cannot_enable_tools_that_write_for_the_automatic_analysis(tmp_path):
    path = tmp_path / "config.toml"
    for ok in ("mcp__claude_ai_Acme__search_docs", "mcp__claude_ai_Atlassian__getJiraIssue",
               "mcp__claude_ai_X__searchDocs"):
        path.write_text(f'[slack.triage]\nallowed_tools = ["{ok}"]\n')
        assert config.load(path).slack_triage.allowed_tools == [ok]
    for bad in ("mcp__claude_ai_Acme__refund_order", "mcp__claude_ai_Atlassian__createJiraIssue",
                "mcp__claude_ai_Slack__slack_send_message", "mcp__claude_ai_X__deleteLead"):
        path.write_text(f'[slack.triage]\nallowed_tools = ["{bad}"]\n')
        with pytest.raises(config.SettingError, match="tool que escribe"):
            config.load(path)


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
