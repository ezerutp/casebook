import json
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
from fastapi.testclient import TestClient

from casebook.config import Config
from casebook.events import Recorder
from casebook.review import ReviewError
from casebook.server import build
from casebook.store import Store
from casebook.viewer.app import create_app


def setup(tmp_path, enabled=True, maximum=3):
    store = Store(tmp_path / 'loops.db')
    sid = store.create_session('review')['id']
    store.review.initialize(sid, enabled, maximum)
    return store, sid


def test_three_rounds_idempotent_and_resume(tmp_path):
    store, sid = setup(tmp_path)
    state = store.review.finish(sid, 'provisional')
    for n in range(1, 4):
        assert state['current'] == n and state['status'] == 'reviewing'
        assert store.review.finish(sid, 'retry')['token'] == state['token']
        token = state['token']
        done = store.review.complete(sid, token, f'confirmed {n}', [], 'unknown')
        assert done['completed'] == n
        state = store.review.advance(sid)
        assert store.review.complete(sid, token, 'retry', []) == state
    assert state['status'] == 'completed'
    assert state['current'] == state['maximum'] == 3
    store.close()
    store = Store(tmp_path / 'loops.db')
    assert store.review.get(sid) == state
    with pytest.raises(ReviewError, match='finalizó'):
        store.review.toggle(sid, True, state['revision'])
    assert store.review.finish(sid, 'retry') == state


def test_toggle_initial_and_during_review(tmp_path):
    store, sid = setup(tmp_path)
    s = store.review.get(sid)
    store.review.toggle(sid, False, s['revision'])
    store.review.query_gate(sid)  # initial investigation is unaffected
    assert store.review.finish(sid, 'answer')['current'] == 0
    other = store.create_session('other')['id']
    s = store.review.initialize(other, False, 2)
    store.review.toggle(other, True, s['revision'])
    s = store.review.finish(other, 'provisional')
    store.review.toggle(other, False, s['revision'])
    with pytest.raises(ReviewError, match='detenido'):
        store.review.query_gate(other)
    s = store.review.complete(other, s['token'], 'partial', [], 'not verified')
    assert s['status'] == 'stopped' and s['completed'] == 0


def test_stop_between_rounds_and_early_finish(tmp_path):
    store, sid = setup(tmp_path)
    s = store.review.finish(sid, 'provisional')
    s = store.review.complete(sid, s['token'], 'review', [])
    store.review.toggle(sid, False, s['revision'])
    s = store.review.advance(sid)
    assert s['status'] == 'stopped' and s['completed'] == 1
    other = store.create_session('limited')['id']
    store.review.initialize(other, True, 3)
    s = store.review.finish(other, 'provisional')
    s = store.review.complete(other, s['token'], 'unknown', [], 'no access', 'No hay acceso')
    assert s['status'] == 'completed' and s['completed'] == 1


def test_evidence_and_stale_tokens_are_rejected(tmp_path):
    store, sid = setup(tmp_path)
    s = store.review.finish(sid, 'provisional')
    for token, ids in [('old-token', []), (s['token'], ['fake'])]:
        with pytest.raises(ReviewError):
            store.review.complete(sid, token, 'reviewed', ids)
    assert store.review.get(sid) == s
    store.add_event(sid, 'query.finished', {}, 'real')
    result = store.review.complete(sid, s['token'], 'confirmed', ['real'])
    assert result['completed'] == 1


def test_cross_process_budget_and_concurrent_completion(tmp_path):
    store, sid = setup(tmp_path)
    store.review.new_analysis(sid, 2, 'job-1')
    second = Store(store.path)
    store.review.query_gate(sid)
    second.review.query_gate(sid)
    with pytest.raises(ReviewError, match='agotado'):
        store.review.query_gate(sid)
    s = store.review.finish(sid, 'provisional')
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(db.review.complete, sid, s['token'], 'reviewed', []) for db in (store, second)]
        for f in futures:
            f.result()
    assert store.review.get(sid)['completed'] == 1
    stale = store.review.get(sid)
    second.review.toggle(sid, False, stale['revision'])
    # A stale revision no longer rejects the switch: the latest explicit choice wins.
    assert store.review.toggle(sid, True, stale['revision'])['enabled'] is True


def test_api_protections_and_conflict(tmp_path):
    store, sid = setup(tmp_path)
    client = TestClient(create_app(store, Config()))
    url = f'/api/sessions/{sid}/loop'
    payload = {'enabled': False, 'revision': store.review.get(sid)['revision']}
    assert client.put(url, json=payload).status_code == 403
    headers = {'x-casebook-viewer': '1'}
    assert client.put(url, json=payload, headers={**headers, 'origin': 'https://evil.test'}).status_code == 403
    assert client.put(url, json=payload, headers=headers).status_code == 200
    assert client.put(url, json=payload, headers=headers).status_code == 200  # idempotent, even with old revision
    assert client.get('/api/sessions').json()[0]['loop']['enabled'] is False
    assert client.put(url, json={'enabled': True}, headers=headers).json()['enabled'] is True
    store.review.finish(sid, 'done')
    store.review.complete(sid, store.review.get(sid)['token'], 'ok', [], '', 'nada más')
    assert client.put(url, json={'enabled': False}, headers=headers).status_code == 409


def test_mcp_protocol_and_legacy_sessions(tmp_path):
    store = Store(tmp_path / 'legacy.db')
    sid = store.create_session('old')['id']
    rec = Recorder(store)
    mcp = build(rec, None, Config(loop_enabled=True))
    def call(name, **args):
        result = anyio.run(lambda: mcp.call_tool(name, args))
        return json.loads(result.content[0].text)
    call('session_resume', session_id=sid)
    assert call('session_review_status')['enabled'] is False
    s = store.review.get(sid)
    store.review.toggle(sid, True, s['revision'])
    s = call('session_finish', conclusion='provisional')
    assert s['status'] == 'reviewing'
    s = call('session_review_complete', token=s['token'], summary='confirmed', evidence=[], remaining='no logs')
    assert s['status'] == 'completed'


def test_settings_validate_and_preserve_session_defaults(tmp_path):
    from casebook import config
    path = tmp_path / 'config.toml'
    config.save({'loop.enabled': True, 'loop.max_additional_reviews': 3}, path)
    cfg = config.load(path)
    assert cfg.loop_enabled and cfg.loop_max_additional_reviews == 3
    for invalid in (0, -1, 1.5, True, 'abc'):
        with pytest.raises(config.SettingError):
            config.save({'loop.max_additional_reviews': invalid}, path)
    store, sid = setup(tmp_path, maximum=3)
    config.save({'loop.max_additional_reviews': 1}, path)
    assert store.review.get(sid)['maximum'] == 3


def test_source_gate_before_backend_and_after_stop(tmp_path):
    from casebook.tools.common import expected_errors
    from mcp.server.mcpserver.exceptions import ToolError
    store, sid = setup(tmp_path)
    rec = Recorder(store)
    rec.resume_session(sid)
    calls = []
    @expected_errors(rec)
    async def fake_source() -> str:
        calls.append(True)
        return 'ok'
    assert anyio.run(lambda: fake_source(purpose='initial')) == 'ok'
    s = store.review.finish(sid, 'provisional')
    store.review.toggle(sid, False, s['revision'])
    with pytest.raises(ToolError, match='detenido'):
        anyio.run(lambda: fake_source(purpose='extra'))
    assert len(calls) == 1


def test_slack_loop_runs_three_rounds_and_keeps_shared_budget(tmp_path):
    from test_slack_watch import watcher, FakeRunner, ANALYSIS
    from casebook import slack_watch
    runner = FakeRunner(ANALYSIS, {**ANALYSIS, 'summary': 'round one'},
                        {**ANALYSIS, 'summary': 'round two'}, {**ANALYSIS, 'summary': 'round three'})
    w, store = watcher(tmp_path, runner, loop_enabled=True, loop_max_additional_reviews=3)
    sid = w.analyze(slack_watch.Job('2.000000', '2.000000', 'U1', 'Ana', 'Investigate'))
    s = store.review.get(sid)
    assert s['completed'] == 3 and s['status'] == 'completed'
    assert len(runner.calls) == 4
    assert 'REVISIÓN ADICIONAL 2/3' in runner.calls[2]['prompt']
    assert s['budget'] == w.cfg.slack_triage.max_queries
    final = [e for e in store.events(sid) if e['type'] == 'slack'][-1]['payload']['result']
    assert final['summary'] == 'round three'


def test_slack_loop_failure_keeps_provisional_result(tmp_path):
    from test_slack_watch import watcher, FakeRunner, ANALYSIS
    from casebook import slack_watch
    runner = FakeRunner(ANALYSIS, slack_watch.ClaudeError('timeout'))
    w, store = watcher(tmp_path, runner, loop_enabled=True)
    sid = w.analyze(slack_watch.Job('2.000000', '2.000000', 'U1', 'Ana', 'Investigate'))
    assert store.review.get(sid)['status'] == 'stopped'
    final = [e for e in store.events(sid) if e['type'] == 'slack'][-1]['payload']['result']
    assert 'Falta una venta' in final['summary'] and 'incompleta' in final['summary']


def test_slack_loop_budget_does_not_reset_between_rounds(tmp_path):
    from test_slack_watch import watcher, ANALYSIS
    from casebook import slack_watch
    calls = []
    def runner(*args, **kwargs):
        calls.append(kwargs)
        sid = store.list_sessions()[0]['id']
        store.review.query_gate(sid)
        return ANALYSIS
    w, store = watcher(tmp_path, runner, loop_enabled=True, loop_max_additional_reviews=3)
    w.cfg.slack_triage.max_queries = 2
    sid = w.analyze(slack_watch.Job('2.000000', '2.000000', 'U1', 'Ana', 'Investigate'))
    assert len(calls) == 2
    s = store.review.get(sid)
    assert s['used'] == 2 and s['completed'] == 1 and s['status'] == 'stopped'


def test_explicit_reopen_keeps_history_and_does_not_repeat(tmp_path):
    store, sid = setup(tmp_path, enabled=False)
    store.review.finish(sid, 'answer')
    with pytest.raises(ReviewError, match='cerrado'):
        store.review.query_gate(sid)
    state = store.review.reopen(sid, 'request-2', 'new evidence from user')
    assert state['status'] == 'initial'
    store.review.query_gate(sid)
    assert store.review.reopen(sid, 'request-2', 'retry') == state
    store.review.finish(sid, 'new answer')
    assert store.review.reopen(sid, 'request-2', 'retry')['status'] == 'completed'
    assert any('new evidence' in e['payload'].get('text', '') for e in store.events(sid))


def test_toggle_during_slack_queries_is_not_rejected(tmp_path):
    store, sid = setup(tmp_path)
    store.review.new_analysis(sid, 10, 'job-1')
    revision = store.review.get(sid)['revision']
    store.review.query_gate(sid)  # Slack accounting bumps the revision
    assert store.review.toggle(sid, False, revision)['enabled'] is False


def test_finished_slack_session_can_be_reopened_by_hand(tmp_path):
    store, sid = setup(tmp_path, enabled=False)
    store.review.new_analysis(sid, 5, 'job-1')
    store.review.finish(sid, 'automatic answer')
    with pytest.raises(ReviewError, match='cerrado'):
        store.review.query_gate(sid)
    state = store.review.reopen(sid, 'manual-1', 'el analista pide revisar otra cuenta')
    assert state['status'] == 'initial' and state['budget'] is None and 'run_id' not in state
    store.review.query_gate(sid)
    assert store.review.finish(sid, 'manual answer')['status'] == 'completed'
    assert any(e['payload'].get('text', '').startswith('Conclusión\n') for e in store.events(sid))
    # A later thread follow-up still starts its own automatic cycle.
    assert store.review.new_analysis(sid, 5, 'job-2')['status'] == 'initial'


def test_reopen_is_still_refused_during_an_active_slack_round(tmp_path):
    store, sid = setup(tmp_path)
    store.review.new_analysis(sid, 5, 'job-1')
    store.review.finish(sid, 'provisional')
    with pytest.raises(ReviewError, match='sigue abierta'):
        store.review.reopen(sid, 'manual-1', 'more')


def test_skipped_follow_up_can_be_retried(tmp_path):
    from test_slack_watch import watcher, FakeRunner, ANALYSIS
    from casebook import slack_watch
    w, store = watcher(tmp_path, FakeRunner(ANALYSIS, ANALYSIS), loop_enabled=False)
    sid = w.analyze(slack_watch.Job('2.000000', '2.000000', 'U1', 'Ana', 'Investigate'))
    w.cfg.slack_max_analyses_per_day = 0
    job = slack_watch.Job('3.000000', '2.000000', 'U1', 'Ana', 'otra', follow_up=True, session_id=sid)
    w.analyze(job)
    assert store.review.get(sid).get('run_id') == '2.000000'  # the skip did not claim the run
    w.cfg.slack_max_analyses_per_day = 100
    assert w.analyze(job) == sid
    assert store.review.get(sid)['run_id'] == '3.000000'


def test_unexpected_slack_failure_halts_loop(tmp_path):
    from test_slack_watch import watcher, FakeRunner, ANALYSIS
    from casebook import slack_watch
    w, store = watcher(tmp_path, FakeRunner(ANALYSIS), loop_enabled=True)
    def boom(*args, **kwargs):
        raise RuntimeError('bug')
    w._review_analysis = boom
    with pytest.raises(RuntimeError):
        w.analyze(slack_watch.Job('2.000000', '2.000000', 'U1', 'Ana', 'Investigate'))
    sid = store.list_sessions()[0]['id']
    assert store.review.get(sid)['status'] == 'stopped'
