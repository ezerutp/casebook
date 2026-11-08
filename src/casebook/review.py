"""Persistent review protocol. Transactions serialize decisions across MCP and viewer processes."""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

from .store import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_loops (
 session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
 data TEXT NOT NULL
);
"""


class ReviewError(ValueError):
    pass


class Review:
    def __init__(self, store):
        self.store = store

    def get(self, sid):
        row = self.store._conn.execute('SELECT data FROM review_loops WHERE session_id=?', (sid,)).fetchone()
        return json.loads(row[0]) if row else None

    @contextmanager
    def change(self, sid):
        with self.store._lock:
            db = self.store._conn
            db.execute('BEGIN IMMEDIATE')
            try:
                if not db.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone():
                    raise ReviewError('sesión no encontrada')
                state = self.get(sid)
                before = json.dumps(state)
                yield state
                if state is not None and json.dumps(state) != before:
                    state['revision'] += 1
                    db.execute('UPDATE review_loops SET data=? WHERE session_id=?', (json.dumps(state), sid))
                    self._event(sid, 'loop.updated', {'loop': state})
                db.execute('COMMIT')
            except Exception:
                db.execute('ROLLBACK')
                raise

    def _event(self, sid, kind, payload):
        self.store._conn.execute('INSERT INTO events(session_id,type,ts,payload) VALUES(?,?,?,?)',
                                 (sid, kind, now(), json.dumps(payload, ensure_ascii=False)))

    def _note(self, sid, text):
        self._event(sid, 'note', {'text': text})

    def initialize(self, sid, enabled=False, maximum=1, budget=None):
        if type(maximum) is not int or maximum < 1:
            raise ReviewError('el máximo de revisiones debe ser un entero positivo')
        with self.change(sid) as existing:
            if existing is None:
                state = dict(enabled=bool(enabled), maximum=maximum, current=0, completed=0,
                             status='initial', revision=0, token=None, results={}, reason='',
                             budget=budget, used=0)
                self.store._conn.execute('INSERT INTO review_loops VALUES(?,?)', (sid, json.dumps(state)))
                self._event(sid, 'loop.updated', {'loop': state})
        return self.get(sid)

    def toggle(self, sid, enabled, revision=None):
        # Sets the desired value: concurrent query accounting must not make the switch fail. `revision` is
        # accepted for compatibility with older viewers and ignored.
        with self.change(sid) as s:
            if s is None:
                raise ReviewError('reanuda la sesión antes de configurar su loop')
            if s['status'] in ('completed', 'stopped'):
                raise ReviewError('la investigación ya finalizó; el toggle no la reinicia')
            s['enabled'] = enabled
            if s['status'] in ('reviewing', 'stopping'):
                s['status'] = 'reviewing' if enabled else 'stopping'
        return self.get(sid)

    def finish(self, sid, conclusion):
        if not conclusion.strip():
            raise ReviewError('indica una conclusión provisional')
        with self.change(sid) as s:
            if s is None:
                raise ReviewError('reanuda la sesión para inicializar su loop')
            if s['status'] == 'initial':
                if not s.get('run_id') or s['enabled']:
                    self._note(sid, ('Conclusión provisional' if s['enabled'] else 'Conclusión') + '\n\n' + conclusion)
                if s['enabled']:
                    self._next(sid, s)
                else:
                    s.update(status='completed', reason='Loop desactivado')
            elif s['status'] == 'stopping':
                s.update(status='stopped', reason='Detenido por el usuario')
                self._note(sid, 'Loop detenido por el usuario. Se conserva la evidencia; revisión incompleta.')
        return self.get(sid)

    def _next(self, sid, s):
        s.update(current=s['completed'] + 1, token=uuid.uuid4().hex, status='reviewing')
        self._note(sid, f"Loop · revisión {s['current']}/{s['maximum']} iniciada. Contrastar las conclusiones con evidencia.")

    def complete(self, sid, token, summary, evidence, remaining='', stop_reason=''):
        if not summary.strip():
            raise ReviewError('resume lo confirmado y corregido')
        with self.change(sid) as s:
            if s is None:
                raise ReviewError('no hay loop')
            if token in s['results']:
                return self.get(sid)  # retry: never advances the next round
            if s['status'] not in ('reviewing', 'stopping') or token != s['token']:
                raise ReviewError('ronda obsoleta o no activa; consulta session_review_status')
            for qid in evidence:
                row = self.store._conn.execute("SELECT 1 FROM events WHERE session_id=? AND query_id=? AND type='query.finished'",
                                               (sid, qid)).fetchone()
                if not row:
                    raise ReviewError(f'evidencia no válida en esta sesión: {qid}')
            s['results'][token] = dict(summary=summary, evidence=evidence, remaining=remaining)
            if s['status'] == 'stopping':
                s.update(status='stopped', reason='Detenido por el usuario; ronda incompleta')
            else:
                s['completed'] = s['current']
                if stop_reason or s['completed'] >= s['maximum']:
                    s.update(status='completed', reason=stop_reason or 'Máximo de revisiones alcanzado')
                else:
                    s['status'] = 'pending'
            self._note(sid, f"Loop · revisión {s['current']}/{s['maximum']}\n\n{summary}\n\n"
                       f"Evidencia: {', '.join(evidence) or 'sin consultas citadas; no implica verificación'}\n"
                       f"Pendientes: {remaining or 'ninguno declarado'}\n{s['reason']}")
            # Return pending: starting another round requires a separate, current-state check.
        return self.get(sid)

    def advance(self, sid):
        with self.change(sid) as s:
            if s and s['status'] == 'pending':
                if s['enabled']:
                    self._next(sid, s)
                else:
                    s.update(status='stopped', reason='Detenido por el usuario entre revisiones')
                    self._note(sid, 'Loop detenido entre revisiones; se conserva la evidencia.')
        return self.get(sid)

    def halt(self, sid, reason):
        with self.change(sid) as s:
            if s and s['status'] not in ('completed', 'stopped'):
                s.update(status='stopped', reason=reason)
                self._note(sid, 'Loop finalizado sin completar todas las revisiones: ' + reason)
        return self.get(sid)

    def query_gate(self, sid):
        """Reserve a source call atomically, including calls from separate MCP processes."""
        with self.change(sid) as s:
            if s is None:
                return
            if s['status'] in ('stopping', 'stopped', 'completed'):
                raise ReviewError('loop detenido o cerrado: conserva la evidencia y solicita session_finish; una petición nueva requiere session_reopen')
            if s['status'] == 'pending':
                raise ReviewError('consulta session_review_status antes de otra revisión')
            if s['budget'] is not None:
                if s['used'] >= s['budget']:
                    raise ReviewError('presupuesto global de consultas agotado; cierra con las limitaciones encontradas')
                s['used'] += 1

    def new_analysis(self, sid, budget, run_id):
        """Only the Slack orchestrator starts a new cycle for an explicit follow-up job."""
        with self.change(sid) as s:
            if s.get('run_id') == run_id and s['status'] != 'stopped':
                raise ReviewError('este análisis ya comenzó; no se repite automáticamente')
            s.update(status='initial', current=0, completed=0, token=None, results={}, reason='',
                     budget=budget, used=0, run_id=run_id)
        return self.get(sid)

    def reopen(self, sid, request_id, reason):
        """Explicit new user request, never a side effect of resume or a toggle."""
        if not request_id.strip() or not reason.strip():
            raise ReviewError('indica un request_id estable y el motivo de la nueva investigación')
        with self.change(sid) as s:
            if s is None:
                raise ReviewError('reanuda la sesión primero')
            if s.get('reopen_id') == request_id:
                return self.get(sid)
            if s['status'] not in ('completed', 'stopped'):
                raise ReviewError('la investigación sigue abierta; continúa su ronda actual')
            # A finished Slack analysis can be continued by hand; it leaves the orchestrator's budget behind.
            s.pop('run_id', None)
            s.update(status='initial', current=0, completed=0, token=None, results={}, reason='',
                     budget=None, used=0, reopen_id=request_id)
            self._note(sid, 'Investigación reabierta por petición explícita: ' + reason)
        return self.get(sid)
