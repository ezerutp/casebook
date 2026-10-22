import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store


def call(mcp, tool_name, **params):
    result = anyio.run(lambda: mcp.call_tool(tool_name, params))
    return json.loads(result.content[0].text)


def test_resume_after_restart_and_read_other_session(tmp_path):
    path = tmp_path / 'sessions.db'
    store = Store(path)
    old = Recorder(store)
    first = old.start_session('SUP-1', 'SUP-1')['id']
    old.note('Pendiente: revisar upload')
    with old.query('os_search', {}, 'GET leads/_search') as q:
        q.finish(['id'], [[123]])
    second = old.start_session('SUP-2')['id']
    old.note('Otro caso')
    store.close()

    store = Store(path)
    try:
        rec = Recorder(store)
        mcp = build(rec, 'http://viewer')
        assert rec.session_id is None
        sessions = call(mcp, 'session_list', query='sup-1')
        assert [s['id'] for s in sessions['sessions']] == [first]
        resumed = call(mcp, 'session_resume', session_id=first)
        assert resumed['latest_notes'][0]['text'] == 'Pendiente: revisar upload'
        assert resumed['viewer_url'] == f'http://viewer/#/sessions/{first}'
        assert len(store.list_sessions()) == 2
        assert rec.session_id == first
        before = store.latest_event_id()
        other = call(mcp, 'session_read', session_id=second)
        assert other['events'][-1]['payload']['text'] == 'Otro caso'
        assert rec.session_id == first and store.latest_event_id() == before
        anyio.run(lambda: mcp.call_tool('session_note', {'text': 'Seguimos mañana'}))
        assert store.events(first)[-1]['payload']['text'] == 'Seguimos mañana'
        with pytest.raises(ToolError, match='no existe'):
            call(mcp, 'session_resume', session_id=999)
        assert rec.session_id == first
        assert store.events(first)[-2]['type'] == 'health'
    finally:
        store.close()


def test_read_pagination_search_and_no_reexecution(tmp_path):
    store = Store(tmp_path / 'sessions.db')
    try:
        rec = Recorder(store)
        sid = rec.start_session('test')['id']
        rec.note('100% confirmado')
        with rec.query('db_select', {}, 'SELECT id FROM lead') as q:
            q.finish(['id'], [[i] for i in range(5)],
                     tables=[{'name': 'extra', 'columns': ['x'], 'rows': [[9], [8], [7]]}],
                     raw={'huge': 'json'})
        mcp = build(rec, None)
        first = call(mcp, 'session_read', session_id=sid, limit=2)
        assert len(first['events']) == 2 and first['next_after'] is not None
        second = call(mcp, 'session_read', session_id=sid, after=first['next_after'], max_rows=2)
        assert second['next_after'] is None
        event = second['events'][-1]
        result = event['payload']['result']
        assert result['rows'] == [[0], [1]] and result['next_row_offset'] == 2
        assert result['tables'][0]['rows'] == [[9], [8]]
        assert 'raw' not in result and result['raw_available_in_viewer']
        page = call(mcp, 'session_read', session_id=sid, after=event['id'] - 1,
                    limit=1, row_offset=2, max_rows=2)
        assert page['events'][0]['payload']['result']['rows'] == [[2], [3]]
        assert len(store.events(sid)[-1]['payload']['result']['rows']) == 5
        assert 'raw' in store.events(sid)[-1]['payload']['result']
        hits = call(mcp, 'session_search', query='100%')['matches']
        assert len(hits) == 1 and hits[0]['session_id'] == sid
        assert 'payload' not in hits[0]
        assert not call(mcp, 'session_search', query='100_')['matches']
        assert rec.queries_started == 1
        with pytest.raises(ToolError, match='vacía'):
            call(mcp, 'session_search', query=' ')
        with pytest.raises(ToolError, match='no existe'):
            call(mcp, 'session_read', session_id=999)
    finally:
        store.close()


def test_latest_notes_and_process_isolation(tmp_path):
    store = Store(tmp_path / 'sessions.db')
    try:
        one, two = Recorder(store), Recorder(store)
        sid = one.start_session('same')['id']
        for i in range(12):
            one.note(f'nota {i}')
        other = two.start_session('same')['id']
        mcp = build(one, None)
        result = call(mcp, 'session_resume', session_id=sid)
        assert len(result['latest_notes']) == 10 and result['older_notes_available']
        assert result['latest_notes'][0]['text'] == 'nota 2'
        assert two.session_id == other
        two.note('independiente')
        assert store.events(other)[-1]['payload']['text'] == 'independiente'
        page = call(mcp, 'session_list', limit=1)
        assert page['next_offset'] == 1
        assert call(mcp, 'session_list', offset=1, limit=1)['next_offset'] is None
    finally:
        store.close()


def test_sessions_order_tracks_activity_and_survives_reload(tmp_path, monkeypatch):
    # Todos los eventos en el mismo milisegundo: el ID desempata la actividad.
    monkeypatch.setattr('casebook.store.now', lambda: '2026-10-16T12:00:00.000+00:00')
    path = tmp_path / 'activity.db'
    store = Store(path)
    rec = Recorder(store)
    first = rec.start_session('SUP-1')['id']
    second = rec.start_session('SUP-2')['id']
    assert [s['id'] for s in store.list_sessions()] == [second, first]
    rec.resume_session(first)
    assert [s['id'] for s in store.list_sessions()] == [first, second]
    # Otro proceso de Claude sigue trabajando en la segunda sesión.
    other = Recorder(store)
    other.resume_session(second)
    rec.note('Actividad nueva en la primera')
    assert [s['id'] for s in store.list_sessions()] == [first, second]
    other.note('Actividad nueva en la segunda')
    assert [s['id'] for s in store.list_sessions()] == [second, first]
    store.close()
    store = Store(path)
    try:
        assert [s['id'] for s in store.list_sessions()] == [second, first]
    finally:
        store.close()


def test_identify_implicit_session_and_keep_history(tmp_path):
    store = Store(tmp_path / 'sessions.db')
    rec = Recorder(store)
    rec.note('Evidencia inicial')
    sid = rec.session_id
    assert store.get_session(sid)['name'] == f'Sin identificar · {sid}'
    other = Recorder(store)
    other.ensure_session()
    assert store.get_session(other.session_id)['name'] == f'Sin identificar · {other.session_id}'
    mcp = build(rec, None)
    updated = call(mcp, 'session_update', card=' sup-13543 ')
    assert updated['id'] == sid
    assert updated['name'] == updated['card'] == 'SUP-13543'
    assert len(store.list_sessions()) == 2
    events = store.events(sid)
    assert events[1]['payload']['text'] == 'Evidencia inicial'
    assert events[-1]['type'] == 'session.updated'
    assert events[-1]['payload']['previous']['name'] == f'Sin identificar · {sid}'
    updated = call(mcp, 'session_update', name='SUP-13543 · CSV')
    assert updated['card'] == 'SUP-13543'
    assert call(mcp, 'session_list', query='CSV')['sessions'][0]['id'] == sid
    before = store.latest_event_id()
    call(mcp, 'session_update', name='SUP-13543 · CSV')
    assert store.latest_event_id() == before
    for params, message in [({}, 'indica'), ({'name': ' '}, 'vacío'), ({'card': 'invalid'}, 'inválida')]:
        with pytest.raises(ToolError, match=message):
            call(mcp, 'session_update', **params)
    assert store.latest_event_id() == before
    store.close()
    store = Store(tmp_path / 'sessions.db')
    assert store.get_session(sid)['name'] == 'SUP-13543 · CSV'
    assert store.get_session(other.session_id)['card'] is None
    store.close()


def test_legacy_provisional_names_and_update_without_session(tmp_path):
    path = tmp_path / 'sessions.db'
    store = Store(path)
    legacy = store.create_session('sin-nombre-2026-10-16')['id']
    custom = store.create_session('Mi investigación')['id']
    store.close()
    store = Store(path)
    assert store.get_session(legacy)['name'] == f'Sin identificar · {legacy}'
    assert store.get_session(custom)['name'] == 'Mi investigación'
    with pytest.raises(ToolError, match='no hay sesión'):
        call(build(Recorder(store), None), 'session_update', card='SUP-1')
    assert len(store.list_sessions()) == 2
    store.close()
