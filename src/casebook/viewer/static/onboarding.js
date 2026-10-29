// Guía de primeros pasos: un recorrido con foco sobre el visor real, en modo demo. Mientras dura, las sesiones y
// Slack se reemplazan por un caso de ejemplo que se reproduce en vivo (nada se consulta ni se guarda) y al terminar
// todo vuelve como estaba. Usa lo que define app.js (state, applyEvent, openSession, …) y GSAP de static/vendor.

const onboarding = (() => {
  const KEY = "casebook-onboarding-v1";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const D = (s) => (reduced ? 0 : s);
  const sleep = (ms) => new Promise((r) => setTimeout(r, reduced ? Math.min(ms, 150) : ms));
  const waitFor = (fn, ms = 4000) =>
    new Promise((resolve) => {
      const start = performance.now();
      const tick = () => {
        const v = fn();
        if (v || performance.now() - start > ms) resolve(v);
        else requestAnimationFrame(tick);
      };
      tick();
    });

  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
  } catch {
    // almacenamiento opcional
  }
  function persist(change) {
    saved = { ...saved, ...change };
    try {
      localStorage.setItem(KEY, JSON.stringify(saved));
    } catch {
      // la guía funciona sin persistencia
    }
  }

  // --- datos del caso de ejemplo --------------------------------------------------------------------------

  const CASE_ID = 990001;
  const SLACK_ID = 990002;
  const Q = { os: "a1b2c3d4e5f6", cw: "9f8e7d6c5b4a", scan: "c0ffee123456", db: "0a1b2c3d4e5f" };
  const now = () => new Date().toISOString();
  const day = (d, t) => `2026-10-${d} ${t}`;

  const demo = {
    active: false,
    sessions: [],
    events: new Map(), // session_id -> eventos emitidos
    nextId: 1,
    runs: new Map(), // guion -> su promesa
    chain: Promise.resolve(), // los guiones van uno detrás del otro
    approval: null, // {resolve, done}
    slack: null,
    restore: null,
  };

  function slackState(change = {}) {
    demo.slack = { ...demo.slack, ...change, last_poll: Date.now() / 1000 };
    if (typeof slackPanel !== "undefined") slackPanel.refresh();
  }

  function addSession(s) {
    demo.sessions.unshift({ events: 0, errors: 0, slack: 0, created_at: now(), last_event_at: now(), last_event_id: 0, ...s });
    demo.events.set(s.id, []);
  }

  // Lo mismo que hace el SSE con un evento real: cuenta en la sesión y lo pinta si es la que está abierta.
  function emit(sessionId, type, payload, queryId) {
    const ev = { id: demo.nextId++, session_id: sessionId, type, ts: now(), payload, query_id: queryId ?? null };
    demo.events.get(sessionId).push(ev);
    const s = demo.sessions.find((x) => x.id === sessionId);
    s.events += 1;
    s.last_event_at = ev.ts;
    s.last_event_id = ev.id;
    if (type === "query.failed" || type === "query.rejected") s.errors += 1;
    if (type === "slack") s.slack = 1;
    state.sessions = [...demo.sessions];
    applyEvent(ev, true);
    renderSessions();
    const el = queryId ? document.querySelector(`#timeline [data-key="${queryId}"]`) : $("#timeline").firstElementChild;
    if (el && state.current === sessionId && /^(query\.started|query\.rejected|note|draft|slack)$/.test(type)) {
      gsap.from(el, { y: -14, opacity: 0, duration: D(0.5), ease: "power3.out", clearProps: "all" });
    }
    return ev;
  }

  // Cada guion corre una sola vez aunque se vuelva atrás, después del anterior. Devuelve cuándo termina ese guion.
  function play(key, script = SCRIPTS[key]) {
    if (!demo.runs.has(key)) {
      demo.chain = demo.chain.then(script);
      demo.runs.set(key, demo.chain);
    }
    return demo.runs.get(key);
  }
  const done = (key) => demo.runs.get(key) || Promise.resolve();

  const SCRIPTS = {
    async opensearch() {
      emit(CASE_ID, "query.started", {
        tool: "os_search",
        purpose: "Ventas de Stripe de la cuenta 4821 desde el 14 oct",
        query: JSON.stringify({ query: { bool: { filter: [{ term: { account_id: 4821 } }, { term: { source: "stripe" } },
          { range: { created_at: { gte: "2026-10-13" } } }] } }, size: 50 }),
        params: { index: "sales", env: "prod", size: 50 },
      }, Q.os);
      await sleep(700);
      emit(CASE_ID, "query.progress", { message: "consultando sales en prod…" }, Q.os);
      await sleep(1100);
      emit(CASE_ID, "query.finished", {
        rows: 4, duration_ms: 842, meta: { total_hits: 4, env: "prod" },
        result: {
          columns: ["created_at", "order_id", "email", "amount", "source", "status"],
          rows: [
            [day(13, "18:42:10"), "pi_3Q8x…kd2", "maria.lopez@example.com", 149, "stripe", "attributed"],
            [day(13, "21:05:33"), "pi_3Q8y…a91", "j.perez@example.com", 89, "stripe", "attributed"],
            [day(14, "08:59:51"), "pi_3Q9a…7cc", "laura.g@example.com", 149, "stripe", "attributed"],
            [day(14, "09:03:12"), "pi_3Q9a…f04", "carlos.r@example.com", 249, "stripe", "attributed"],
          ],
        },
      }, Q.os);
    },

    async cloudwatch() {
      emit(CASE_ID, "query.started", {
        tool: "cw_query",
        purpose: "Errores del webhook de Stripe desde el 14 oct",
        query: "fields @timestamp, status, error\n| filter account_id = 4821 and status >= 400\n| sort @timestamp desc\n| limit 100",
        params: { log_group: "/aws/lambda/stripe-webhook", start: "2026-10-14T00:00:00Z", end: "2026-10-21T00:00:00Z", limit: 100 },
      }, Q.cw);
      await sleep(600);
      emit(CASE_ID, "query.progress", { message: "estimado: 3.2 GB a escanear · $0.0160" }, Q.cw);
      await sleep(500);
      emit(CASE_ID, "query.approval_requested", {
        reason: "El estimado ($0.0160 × 1.5 de margen) supera el tope de $0.0100 por consulta de CloudWatch.",
        timeout_s: 300,
      }, Q.cw);
      await new Promise((resolve) => (demo.approval = { resolve }));
    },

    async guards() {
      emit(CASE_ID, "query.rejected", {
        tool: "db_select",
        purpose: "Contar todas las ventas, sin filtro",
        params: { sql: "SELECT COUNT(*) FROM sale" },
        reason: "Full scan estimado de 48 M filas en sale (el tope es 200 000). Filtra por una columna indexada.",
      }, Q.scan);
      await sleep(1300);
      emit(CASE_ID, "query.started", {
        tool: "db_select",
        purpose: "Estado de la integración de Stripe de la cuenta",
        query: "SELECT id, provider, status, last_error, updated_at\nFROM integration\nWHERE account_id = 4821 AND provider = 'stripe'",
        params: { sql: "SELECT id, provider, status, last_error, updated_at FROM integration WHERE account_id = 4821 AND provider = 'stripe'" },
      }, Q.db);
      await sleep(900);
      emit(CASE_ID, "query.finished", {
        rows: 1, duration_ms: 38, meta: { env: "replica" },
        result: {
          columns: ["id", "provider", "status", "last_error", "updated_at"],
          rows: [[18832, "stripe", "error", "Webhook signing secret mismatch", day(14, "09:04:27")]],
        },
      }, Q.db);
    },

    async note() {
      emit(CASE_ID, "note", {
        text: "Las ventas de Stripe de la cuenta 4821 dejan de llegar el 14 oct a las 09:04 (q a1b2c3d4e5f6). Desde ese " +
          "momento el webhook rechaza cada evento por firma inválida (q 9f8e7d6c5b4a) y la integración quedó en error con " +
          "«signing secret mismatch» (q 0a1b2c3d4e5f): el secreto se rotó en Stripe y no se actualizó en la app.",
      });
    },

    async draft() {
      emit(CASE_ID, "draft", {
        card: "CASO-20431",
        text: "Hola, revisamos la cuenta 4821. Las ventas de Stripe desde el 14 de octubre sí existen en Stripe, pero no " +
          "llegaron a la app: el webhook rechaza cada evento por firma inválida porque el signing secret se rotó en Stripe " +
          "y no se actualizó en la app.\n\nPasos: 1) copiar el nuevo signing secret del endpoint en Stripe; 2) pegarlo en " +
          "Integraciones → Stripe; 3) reenviar desde Stripe los eventos fallidos. Las ventas aparecen con su fecha original.",
      });
    },

    async slack() {
      slackState({ analyzing: true });
      await sleep(1600);
      addSession({ id: SLACK_ID, name: "Slack · Ana Ruiz", card: null, slack: 1 });
      emit(SLACK_ID, "slack", {
        kind: "message", author: "Ana Ruiz",
        text: "Cliente de la cuenta 7310 pregunta por qué el reporte muestra 0 clicks de Google Ads ayer si la campaña " +
          "tuvo tráfico. ¿Es de nuestro lado?",
        files: [{ id: "F07DEMO", name: "reporte-ads.png", mimetype: "image/png" }],
      });
      emit(SLACK_ID, "slack", {
        kind: "analysis", outcome: "explain", outcome_label: "respuesta directa",
        result: {
          category: "Atribución · Google Ads",
          summary: "Los clicks sí llegaron, pero sin gclid: la campaña nueva no tiene el etiquetado automático activo.",
          hypothesis: "Desde ayer los clicks entran como tráfico orgánico porque la URL final no trae gclid.",
          missing_data: "Captura de la configuración de la cuenta de Google Ads.",
          steps: ["Confirmar en Google Ads → Configuración de la cuenta que el etiquetado automático está activo.",
            "Revisar que la URL final no tenga redirecciones que quiten parámetros.",
            "Esperar unas horas: los clicks nuevos se atribuyen solos."],
          attachments_reviewed: [{ file_id: "F07DEMO", description: "reporte de Ads con 0 clicks el 20 oct" }],
          suggested_reply: "Hola Ana: no es un problema de la app. Los clicks de ayer llegaron sin gclid porque la campaña " +
            "nueva no tiene el etiquetado automático de Google Ads. Con activarlo, los clicks siguientes se atribuyen bien.",
        },
      });
      slackState({ analyzing: false, today: 1 });
    },
  };

  // Aprobar o rechazar la consulta de CloudWatch. Desde la tarjeta, la guía pasa sola al paso siguiente.
  async function resolveApproval(approve, advance = true) {
    if (!demo.approval || demo.approval.done) return;
    demo.approval.done = true;
    emit(CASE_ID, approve ? "query.approved" : "query.denied", {}, Q.cw);
    await sleep(700);
    if (approve) {
      emit(CASE_ID, "query.progress", { message: "corriendo en /aws/lambda/stripe-webhook" }, Q.cw);
      await sleep(1200);
      emit(CASE_ID, "query.finished", {
        rows: 3, duration_ms: 5230, meta: { cost_usd: 0.0154, env: "aws" },
        result: {
          columns: ["@timestamp", "status", "error"],
          rows: [
            [day(14, "09:04:27.118"), "400", "No signatures found matching the expected signature for payload"],
            [day(14, "09:11:02.540"), "400", "No signatures found matching the expected signature for payload"],
            [day(20, "17:48:19.003"), "400", "No signatures found matching the expected signature for payload"],
          ],
        },
      }, Q.cw);
    } else {
      emit(CASE_ID, "query.failed", { error: "Rechazada en el visor: no se corrió.", duration_ms: 0 }, Q.cw);
    }
    demo.approval.resolve();
    if (advance) setTimeout(() => tour.step()?.id === "approval" && tour.next(), reduced ? 0 : 600);
  }

  function exportMd(id) {
    const s = demo.sessions.find((x) => x.id === id);
    const notes = (demo.events.get(id) || []).filter((e) => e.type === "note").map((e) => `- ${e.payload.text}`);
    return `# ${s?.name || "Sesión de ejemplo"}\n\n_Sesión de la guía de primeros pasos._\n\n## Conclusiones\n\n${notes.join("\n") || "—"}\n`;
  }

  // --- servidor de mentira --------------------------------------------------------------------------------

  const realFetch = window.fetch.bind(window);
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  const blocked = (what) => json({ detail: `Estás en la demo de la guía: ${what}. Termina la guía para hacerlo de verdad.` }, 409);

  window.fetch = (input, init = {}) => {
    if (!demo.active) return realFetch(input, init);
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const method = (init.method || "GET").toUpperCase();
    const path = url.pathname;
    let m;
    if (path === "/api/sessions") return Promise.resolve(json(demo.sessions));
    if ((m = path.match(/^\/api\/sessions\/(\d+)\/events$/))) return Promise.resolve(json(demo.events.get(Number(m[1])) || []));
    if ((m = path.match(/^\/api\/sessions\/(\d+)\/export\.md$/))) return Promise.resolve(new Response(exportMd(Number(m[1]))));
    if (path === "/api/search") return Promise.resolve(json([]));
    if (path === "/api/slack") return Promise.resolve(json(demo.slack));
    if (path.startsWith("/api/slack/thread/") && method === "GET") return Promise.resolve(json({ has_thread: false, check: null }));
    if (path.startsWith("/api/slack/")) return Promise.resolve(blocked("Slack no se lee"));
    if ((m = path.match(/^\/api\/queries\/(\w+)\/approval$/))) {
      const { approve } = JSON.parse(init.body || "{}");
      if (m[1] === Q.cw) resolveApproval(!!approve);
      return Promise.resolve(json({}));
    }
    if (path.startsWith("/api/queries/")) return Promise.resolve(json({}));
    if (path === "/api/settings" && method !== "GET") return Promise.resolve(blocked("los ajustes no se guardan"));
    return realFetch(input, init);
  };

  // Descargar el .md de una sesión de ejemplo llevaría a un 404 del servidor real.
  document.addEventListener("click", (e) => {
    if (demo.active && e.target.closest?.('a[href^="/api/"]')) e.preventDefault();
  }, true);

  // --- entrar y salir del modo demo -----------------------------------------------------------------------

  async function enterDemo() {
    demo.restore = { hash: location.hash, filter: state.sessionFilter, search: $("#search").value };
    Object.assign(demo, { active: true, sessions: [], events: new Map(), nextId: 1, runs: new Map(), chain: Promise.resolve(), approval: null });
    demo.slack = { enabled: true, listening: true, analyzing: false, queued: 0, today: 0, max_per_day: 20,
      backup_poll_minutes: 15, recent: [], manual: null, last_error: null, listener_error: null, last_poll: Date.now() / 1000 };

    // Lo real se va con una animación y queda el visor vacío, como recién instalado.
    const out = [...document.querySelectorAll("#sessions > a")];
    const main = [$("#sessions-view"), $("#settings-view"), $("#skills-view"), $("#knowledge-view")].filter((el) => !el.hidden);
    await gsap.timeline()
      .to(out, { x: -18, opacity: 0, duration: D(0.28), stagger: D(0.025), ease: "power2.in" })
      .to(main, { opacity: 0, y: 8, duration: D(0.25), ease: "power2.in" }, "<0.05");

    if (cardModal.open) cardModal.close();
    $("#search").value = "";
    runSearch("");
    state.sessionFilter = { origin: "all", errors: false, week: false };
    state.sessions = [];
    state.current = null;
    location.hash = "#/sessions";
    await openSession(null);
    renderSessions();
    slackState();
    gsap.set([...out, ...main], { clearProps: "all" });
    gsap.fromTo($("#sessions-view"), { opacity: 0, y: 8 }, { opacity: 1, y: 0, duration: D(0.4), ease: "power2.out", clearProps: "all" });
  }

  async function exitDemo() {
    if (!demo.active) return;
    if (cardModal.open) cardModal.close();
    demo.active = false;
    const { hash, filter, search } = demo.restore;
    state.sessionFilter = filter;
    state.current = null;
    await loadSessions();
    history.replaceState(null, "", hash || "#/sessions");
    const id = sessionFromHash() ?? state.sessions[0]?.id ?? null;
    if (currentView() === "sessions" && sessionFromHash() == null && id != null) history.replaceState(null, "", `#/sessions/${id}`);
    showView(currentView());
    await openSession(id);
    route();
    slackPanel.refresh();
    if (search) {
      $("#search").value = search;
      runSearch(search);
    }
    gsap.from("#sessions > a", { x: -12, opacity: 0, duration: D(0.35), stagger: D(0.02), ease: "power2.out", clearProps: "all" });
  }

  async function showSession(id) {
    if (currentView() !== "sessions" || sessionFromHash() !== id) location.hash = `#/sessions/${id}`;
    const last = demo.events.get(id)?.at(-1)?.id || 0;
    await waitFor(() => state.current === id && state.lastEventId >= last && $("#session-head .stats"));
  }

  async function goView(hash, ready) {
    if (!location.hash.startsWith(hash)) location.hash = hash;
    await waitFor(ready);
  }

  const card = (id) => () => document.querySelector(`#timeline [data-key="${id}"]`);

  // --- pasos ----------------------------------------------------------------------------------------------

  const STEPS = [
    {
      id: "intro",
      kicker: "Modo demo",
      title: "Vamos a investigar un caso de ejemplo",
      text: "Ocultamos tus sesiones y la actividad de Slack mientras dura la guía. Vas a ver llegar un caso, cómo corren sus " +
        "consultas, cuánto cuestan y dónde queda la respuesta.",
      hint: "No se consulta ningún servicio ni se guarda nada. Al terminar, todo vuelve como estaba.",
      next: "Empezar",
    },
    {
      id: "session",
      kicker: "Sesiones",
      title: "Llega un caso nuevo",
      text: "Cuando le pides a Claude «Investiga CASO-20431», la sesión aparece aquí. Con «Seguir la sesión activa» marcado, " +
        "el visor la abre solo.",
      target: () => document.querySelector(`#sessions a[href="#/sessions/${CASE_ID}"]`),
      place: "right",
      async enter() {
        if (!demo.sessions.some((s) => s.id === CASE_ID)) {
          addSession({ id: CASE_ID, name: "CASO-20431", card: "CASO-20431" });
          await loadSessions();
          const a = await waitFor(() => document.querySelector(`#sessions a[href="#/sessions/${CASE_ID}"]`));
          if (a) gsap.from(a, { x: -24, opacity: 0, duration: D(0.6), ease: "back.out(1.8)", clearProps: "all" });
        }
        await showSession(CASE_ID);
      },
    },
    {
      id: "query",
      kicker: "Consultas en vivo",
      title: "Cada consulta llega mientras corre",
      text: "La tarjeta muestra el servicio, la tool, el propósito que escribió Claude y su estado. Con «Parámetros» y " +
        "«Copiar consulta» puedes repetirla tú.",
      target: card(Q.os),
      place: "bottom",
      async enter() {
        await showSession(CASE_ID);
        play("opensearch");
      },
    },
    {
      id: "result",
      kicker: "Resultados",
      title: "Explora la evidencia",
      text: "Ordena por cualquier columna, filtra filas, copia CSV o cambia a JSON. Los correos se ocultan en pantalla; " +
        "«Mostrar correos» los revela solo en esta tarjeta.",
      target: () => card(Q.os)()?.querySelector(".result:not([hidden])"),
      place: "left",
      async enter() {
        await showSession(CASE_ID);
        await play("opensearch");
      },
    },
    {
      id: "approval",
      kicker: "Aprobación",
      title: "Las consultas caras te piden permiso",
      text: "Esta búsqueda en CloudWatch supera el tope de costo, así que espera tu decisión. Lee el motivo y elige: si no " +
        "respondes, se rechaza sola al vencer el plazo.",
      hint: "Pruébalo: pulsa «Aprobar y correr» en la tarjeta.",
      next: "Aprobar por mí",
      target: () => card(Q.cw)()?.querySelector(".approval:not([hidden])") || card(Q.cw)(),
      place: "bottom",
      async enter() {
        await showSession(CASE_ID);
        await play("opensearch");
        play("cloudwatch");
        await waitFor(() => card(Q.cw)()?.querySelector(".approval:not([hidden])") || demo.approval?.done);
      },
      async beforeNext() {
        await resolveApproval(true, false);
      },
    },
    {
      id: "costs",
      kicker: "Gastos",
      title: "Lo que lleva gastado la sesión",
      text: "Cada servicio cuenta sus consultas, las que siguen en curso y los errores. CloudWatch cobra por GB escaneado, " +
        "así que también suma su costo en dólares; cada tarjeta muestra el suyo.",
      target: () => $("#session-head .stats"),
      place: "bottom",
      async enter() {
        await showSession(CASE_ID);
        await done("cloudwatch");
      },
    },
    {
      id: "guards",
      kicker: "Guardas",
      title: "Lo peligroso no llega a correr",
      text: "Antes de tocar la réplica, cada SELECT pasa por un EXPLAIN. Un full scan enorme se bloquea con el motivo y " +
        "Claude reintenta filtrando por una columna indexada.",
      target: card(Q.scan),
      place: "bottom",
      async enter() {
        await showSession(CASE_ID);
        await done("cloudwatch");
        play("guards");
      },
    },
    {
      id: "note",
      kicker: "Conclusiones",
      title: "Cada hallazgo con su evidencia",
      text: "Claude deja notas a medida que concluye. Las referencias a consultas se convierten en pills: ábrelas para ver " +
        "la tarjeta que respalda la frase sin perder tu lugar.",
      hint: "Pruébalo: pulsa una de las pills de la nota.",
      target: () => $("#timeline .card.note"),
      place: "bottom",
      async enter() {
        await showSession(CASE_ID);
        await done("guards");
        play("note");
      },
    },
    {
      id: "summary",
      kicker: "Respuesta",
      title: "El borrador para la card",
      text: "La columna de resumen junta las conclusiones, el borrador y las fuentes consultadas. Copia el borrador para " +
        "Jira: nunca se publica solo. «Copiar Markdown» se lleva la investigación completa.",
      target: () => $("#summary:not([hidden])"),
      place: "left",
      async enter() {
        await showSession(CASE_ID);
        await done("note");
        play("draft");
      },
    },
    {
      id: "filters",
      kicker: "Filtros",
      title: "Encuentra una tarjeta en segundos",
      text: "Filtra el timeline por servicio, notas o errores, o busca texto dentro de la sesión. En la barra lateral, " +
        "busca en todas las sesiones a la vez.",
      target: () => $("#session-head .card-filters"),
      place: "bottom",
      async enter() {
        await showSession(CASE_ID);
      },
    },
    {
      id: "slack",
      kicker: "Slack",
      title: "Pedidos del canal de soporte",
      text: "Con la vigilancia encendida, cada pedido nuevo del canal se analiza con la skill de triage y queda como una " +
        "sesión. Solo lee: nunca escribe en Slack.",
      hint: "Mira cómo llega un pedido nuevo abajo.",
      target: () => $("#slack-panel:not([hidden])"),
      place: "right",
      async enter() {
        await showSession(CASE_ID);
        play("slack");
      },
    },
    {
      id: "slack-analysis",
      kicker: "Slack",
      title: "Un análisis listo para responder",
      text: "Categoría, hipótesis, qué falta y los pasos. Si no es de nuestro lado, trae una respuesta sugerida para " +
        "copiar al hilo; si hace falta escalar con una card, también lo dice.",
      target: () => $("#timeline .slack-analysis"),
      place: "left",
      async enter() {
        await play("slack");
        await showSession(SLACK_ID);
      },
    },
    {
      id: "connections",
      kicker: "Conexiones",
      title: "Antes de investigar, revisa esto",
      text: "OpenSearch, CloudWatch y MySQL con su estado real. Si algo falla, pasa el mouse para ver el detalle o revisa " +
        "de nuevo con el botón.",
      target: () => $("#conn-list")?.closest(".conn-panel"),
      place: "right",
    },
    {
      id: "settings",
      kicker: "Ajustes",
      title: "Tú pones los límites",
      text: "El tope de CloudWatch por consulta, el plazo para aprobar y los límites de MySQL viven aquí. Lo que los supera " +
        "pide tu aprobación o se rechaza.",
      hint: "En la demo los cambios no se guardan.",
      target: () => $('.field[data-key="cloudwatch.max_cost_usd"]'),
      place: "bottom",
      async enter() {
        await goView("#/settings/guardas", () => $('.field[data-key="cloudwatch.max_cost_usd"]'));
      },
    },
    {
      id: "knowledge",
      kicker: "Conocimiento",
      title: "Lo aprendido no se pierde",
      text: "Esquemas, relaciones y referencias que se guardaron en otras investigaciones, con su evidencia. Filtra por " +
        "fuente, entorno o estado antes de reutilizarlos.",
      target: () => $("#knowledge-filters"),
      place: "bottom",
      async enter() {
        await goView("#/knowledge", () => !$("#knowledge-view").hidden && $("#knowledge-filters"));
      },
    },
    {
      id: "skills",
      kicker: "Skills",
      title: "Cómo investiga Claude",
      text: "Las skills le dicen al agente cómo encarar cada caso. Revisa cuáles están instaladas y su estado; un aviso en " +
        "la barra lateral te dice si alguna necesita atención.",
      target: () => $(".skills-list"),
      place: "right",
      async enter() {
        await goView("#/skills", () => !$("#skills-view").hidden && $(".skills-list"));
      },
    },
    {
      id: "done",
      kicker: "Listo",
      title: "Ya puedes empezar",
      text: "Vuelve a tu chat con Claude, describe el caso y sigue la investigación aquí. Puedes repetir esta guía cuando " +
        "quieras desde Ajustes → Primeros pasos.",
      next: "Volver a mis sesiones",
    },
  ];

  // --- recorrido: velo con hueco, anillo y popover --------------------------------------------------------

  const tour = (() => {
    const root = document.createElement("div");
    root.className = "tour";
    root.hidden = true;
    root.innerHTML = `
      <div class="tour-veil"></div>
      <div class="tour-ring"></div>
      <div class="tour-badge" role="status"><span class="tour-pulse" aria-hidden="true"></span>
        <span>Modo demo · tus sesiones vuelven al terminar</span><button class="link-btn tour-quit">Salir</button></div>
      <section class="tour-pop" role="dialog" aria-modal="false" aria-labelledby="tour-title" aria-describedby="tour-text">
        <span class="tour-arrow" aria-hidden="true"></span>
        <div class="tour-top"><span class="tour-kicker"></span><span class="tour-count"></span>
          <button class="icon-btn tour-x" aria-label="Salir de la guía">${ICONS.close}</button></div>
        <div class="tour-bar" aria-hidden="true"><span></span></div>
        <div class="tour-body" aria-live="polite">
          <h2 id="tour-title"></h2>
          <p id="tour-text"></p>
          <p class="tour-hint"></p>
        </div>
        <div class="tour-foot"><button class="ghost tour-back">Anterior</button><button class="primary tour-next"></button></div>
      </section>`;
    document.body.append(root);
    const veil = $(".tour-veil", root);
    const ring = $(".tour-ring", root);
    const pop = $(".tour-pop", root);
    const nextBtn = $(".tour-next", root);
    const backBtn = $(".tour-back", root);

    let index = -1;
    let busy = false;
    let target = null;
    const hole = { x: innerWidth / 2, y: innerHeight / 2, w: 0, h: 0 };
    const goal = { ...hole, px: 0, py: 0 };
    const ease = { duration: D(0.6), ease: "power3.out" };
    const holeTo = Object.fromEntries(["x", "y", "w", "h"].map((k) => [k, gsap.quickTo(hole, k, ease)]));
    const popTo = { x: gsap.quickTo(pop, "x", ease), y: gsap.quickTo(pop, "y", ease) };

    function visible(el) {
      if (!el || !el.isConnected) return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }

    function roundedHole({ x, y, w, h }) {
      const W = innerWidth, H = innerHeight;
      const r = Math.max(0, Math.min(12, w / 2, h / 2));
      return `path(evenodd, "M0 0H${W}V${H}H0Z M${x + r} ${y}H${x + w - r}A${r} ${r} 0 0 1 ${x + w} ${y + r}V${y + h - r}` +
        `A${r} ${r} 0 0 1 ${x + w - r} ${y + h}H${x + r}A${r} ${r} 0 0 1 ${x} ${y + h - r}V${y + r}A${r} ${r} 0 0 1 ${x + r} ${y}Z")`;
    }

    // Dónde va el popover: del lado preferido si entra; si no, el primero que entre; si ninguno, abajo a la derecha.
    function place(box, pref) {
      const W = innerWidth, H = innerHeight, m = 16, gap = 16;
      const pw = pop.offsetWidth, ph = pop.offsetHeight;
      if (!box.w) return { x: (W - pw) / 2, y: (H - ph) / 2, side: "none" };
      const cx = box.x + box.w / 2, cy = box.y + box.h / 2;
      const at = {
        right: { x: box.x + box.w + gap, y: cy - ph / 2 },
        left: { x: box.x - gap - pw, y: cy - ph / 2 },
        bottom: { x: cx - pw / 2, y: box.y + box.h + gap },
        top: { x: cx - pw / 2, y: box.y - gap - ph },
      };
      const fits = (side) => {
        const p = at[side];
        return side === "left" || side === "right" ? p.x >= m && p.x + pw <= W - m : p.y >= m && p.y + ph <= H - m;
      };
      const side = [pref, "right", "bottom", "left", "top"].find((s) => s && fits(s));
      if (!side) return { x: W - pw - m, y: H - ph - m, side: "none" };
      const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
      const x = clamp(at[side].x, m, W - pw - m);
      const y = clamp(at[side].y, m, H - ph - m);
      const arrow = side === "left" || side === "right" ? clamp(cy - y, 20, ph - 20) : clamp(cx - x, 20, pw - 20);
      return { x, y, side, arrow };
    }

    // Cada cuadro: el hueco sigue al elemento (que se mueve con el scroll o cuando llegan tarjetas) y el popover a él.
    function frame() {
      const step = STEPS[index];
      if (!step) return;
      const el = step.target?.();
      if (el !== target) target = el;
      let box = { x: innerWidth / 2, y: innerHeight / 2, w: 0, h: 0 };
      if (visible(el)) {
        const r = el.getBoundingClientRect();
        const pad = 6;
        const top = Math.max(8, r.top - pad);
        const bottom = Math.min(innerHeight - 8, r.bottom + pad);
        box = { x: r.left - pad, y: top, w: r.width + pad * 2, h: Math.max(0, bottom - top) };
      }
      for (const k of ["x", "y", "w", "h"]) {
        if (Math.abs(goal[k] - box[k]) > 0.5) {
          goal[k] = box[k];
          holeTo[k](box[k]);
        }
      }
      veil.style.clipPath = roundedHole(hole);
      gsap.set(ring, { x: hole.x, y: hole.y, width: hole.w, height: hole.h, autoAlpha: hole.w > 4 ? 1 : 0 });
      const p = place(box, step.place);
      if (Math.abs(goal.px - p.x) > 0.5 || Math.abs(goal.py - p.y) > 0.5) {
        goal.px = p.x;
        goal.py = p.y;
        popTo.x(p.x);
        popTo.y(p.y);
      }
      pop.dataset.side = p.side;
      if (p.arrow != null) pop.style.setProperty("--arrow", `${p.arrow}px`);
    }

    async function go(i) {
      if (busy || i < 0 || i >= STEPS.length) return;
      busy = true;
      const first = index === -1;
      const step = STEPS[i];
      const body = [...$(".tour-body", root).children];
      if (!first) await gsap.to(body, { opacity: 0, y: -6, duration: D(0.14), stagger: D(0.03), ease: "power1.in" });
      index = i;
      root.dataset.step = step.id;
      $(".tour-kicker", root).textContent = step.kicker;
      $(".tour-count", root).textContent = `${i + 1} de ${STEPS.length}`;
      $("#tour-title").textContent = step.title;
      $("#tour-text").textContent = step.text;
      $(".tour-hint", root).textContent = step.hint || "";
      $(".tour-hint", root).hidden = !step.hint;
      nextBtn.textContent = step.next || "Siguiente";
      backBtn.hidden = i === 0 || i === STEPS.length - 1;
      nextBtn.disabled = true;
      gsap.to($(".tour-bar span", root), { scaleX: (i + 1) / STEPS.length, duration: D(0.6), ease: "power3.inOut" });
      try {
        await step.enter?.();
      } catch (err) {
        console.error("guía:", err);
      }
      const el = await waitFor(() => (step.target ? visible(step.target()) && step.target() : true), 2500);
      if (el instanceof Element) el.scrollIntoView({ block: "center", inline: "nearest", behavior: reduced ? "auto" : "smooth" });
      gsap.fromTo(body.filter((n) => !n.hidden), { opacity: 0, y: 10 },
        { opacity: 1, y: 0, duration: D(0.38), stagger: D(0.06), ease: "power2.out", clearProps: "transform" });
      nextBtn.disabled = false;
      busy = false;
      nextBtn.focus({ preventScroll: true });
    }

    async function next() {
      if (busy) return;
      const step = STEPS[index];
      if (step?.beforeNext) {
        nextBtn.disabled = true;
        await step.beforeNext();
        if (STEPS[index] !== step) return; // la aprobación ya avanzó sola
      }
      if (index === STEPS.length - 1) return close(true);
      go(index + 1);
    }

    async function open() {
      index = -1;
      target = null;
      root.hidden = false;
      document.documentElement.classList.add("touring");
      Object.assign(hole, { x: innerWidth / 2, y: innerHeight / 2, w: 0, h: 0 });
      Object.assign(goal, hole, { px: -1, py: -1 });
      gsap.ticker.add(frame);
      gsap.set(pop, { x: (innerWidth - pop.offsetWidth) / 2, y: (innerHeight - pop.offsetHeight) / 2 });
      gsap.timeline()
        .fromTo(veil, { opacity: 0 }, { opacity: 1, duration: D(0.45), ease: "power2.out" })
        .fromTo(".tour-badge", { y: -30, opacity: 0 }, { y: 0, opacity: 1, duration: D(0.5), ease: "back.out(1.7)" }, "<0.1")
        .fromTo(pop, { opacity: 0, scale: 0.92 }, { opacity: 1, scale: 1, duration: D(0.5), ease: "back.out(1.6)" }, "<0.05");
      await go(0);
    }

    async function close(completed) {
      if (root.hidden) return;
      busy = true;
      persist(completed ? { completed: true } : { dismissed: true });
      $("#onboarding-welcome").hidden = true;
      await gsap.timeline()
        .to(pop, { opacity: 0, scale: 0.94, duration: D(0.25), ease: "power2.in" })
        .to([veil, ring, ".tour-badge"], { opacity: 0, duration: D(0.3) }, "<0.05");
      gsap.ticker.remove(frame);
      root.hidden = true;
      document.documentElement.classList.remove("touring");
      gsap.set([pop, veil, ring, ".tour-badge"], { clearProps: "opacity,scale" });
      index = -1;
      busy = false;
      await exitDemo();
    }

    nextBtn.addEventListener("click", next);
    backBtn.addEventListener("click", () => go(index - 1));
    $(".tour-x", root).addEventListener("click", () => close(false));
    $(".tour-quit", root).addEventListener("click", () => close(false));
    document.addEventListener("keydown", (e) => {
      if (root.hidden || document.querySelector("dialog[open]")) return;
      if (e.key === "Escape") close(false);
      const typing = e.target.closest?.("input, textarea, select, [contenteditable]");
      if (typing) return;
      if (e.key === "ArrowRight") next();
      if (e.key === "ArrowLeft" && !backBtn.hidden) go(index - 1);
    });

    return { open, next, step: () => STEPS[index] };
  })();

  // --- entrada --------------------------------------------------------------------------------------------

  async function start() {
    if (demo.active) return;
    $("#onboarding-welcome").hidden = true;
    await enterDemo();
    await tour.open();
  }

  const welcome = $("#onboarding-welcome");
  welcome.hidden = saved.completed === true || saved.dismissed === true;
  $("#onboarding-start").addEventListener("click", start);
  $("#onboarding-dismiss").addEventListener("click", () => {
    persist({ dismissed: true });
    gsap.to(welcome, { opacity: 0, height: 0, marginBottom: 0, paddingTop: 0, paddingBottom: 0, duration: D(0.3),
      ease: "power2.in", onComplete: () => { welcome.hidden = true; gsap.set(welcome, { clearProps: "all" }); } });
  });

  return {
    start,
    get demo() {
      return demo.active;
    },
  };
})();
