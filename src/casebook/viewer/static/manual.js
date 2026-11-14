// Editor de consultas del ingeniero: «Editar y ejecutar» desde una tarjeta o «+ Consulta» nueva. Elige fuente,
// entorno, tipo y esquema; valida en seco con los mismos guards de las tools (y estima el costo en CloudWatch)
// mientras escribes, y la consulta corre en la sesión como una más (origin "manual").
/* global $, state, action, notice, errorText, ICONS, envTone, focusCard, fillIcons */

const manualEditor = (() => {
  const dlg = $("#manual-editor");
  const TOOLS = {
    os: [{ tool: "os_search", label: "Buscar" }, { tool: "os_count", label: "Contar" }],
    db: [{ tool: "db_select", label: "SELECT" }],
    cw: [{ tool: "cw_query", label: "Logs Insights" }],
  };
  const SVC_OF_TOOL = { os_search: "os", os_count: "os", db_select: "db", cw_query: "cw" };
  const ENTITY = {
    os: { label: "Índice o patrón", placeholder: "leads-*", hint: "Un índice o un patrón con prefijo (sin '*' solo ni índices de sistema)." },
    cw: { label: "Log group", placeholder: "/ecs/mi-servicio", hint: "Un solo log group exacto, sin comodines." },
    db: { label: "Tabla (para la guía)", placeholder: "lead", hint: "Opcional: la tabla que vas a consultar, para ver sus columnas e índices." },
  };
  const OS_KEYS = { query: "query", _source: "source", sort: "sort", aggs: "aggs", size: "size", track_total_hits: "track_total_hits" };

  let opts = null;          // /api/manual/options
  let form = null;          // {svc, env, tool, runMode, parent, sessionId}
  let checkTimer = null;
  let checkSeq = 0;
  let last = null;          // la última validación
  let running = false;

  const el = (sel) => $(sel, dlg);

  async function loadOptions() {
    opts = await fetch("/api/manual/options").then((r) => r.json());
    return opts;
  }

  function sources() {
    return (opts?.sources || []).map((s) => ({ ...s, svc: s.svc }));
  }

  function source(svc) {
    return sources().find((s) => s.svc === svc);
  }

  // --- abrir ---------------------------------------------------------------------------------------------------

  async function openNew(preset = {}) {
    if (state.current == null) return;
    await loadOptions();
    if (!sources().length) {
      notice("No hay fuentes configuradas", "Agrega OpenSearch, MySQL o CloudWatch en Ajustes → Fuentes.", "warn");
      return;
    }
    const svc = preset.svc && source(preset.svc) ? preset.svc : sources()[0].svc;
    form = { svc, env: preset.env || source(svc).envs[0]?.name, tool: preset.tool || TOOLS[svc][0].tool,
             runMode: "new", parent: null, sessionId: state.current };
    el("#manual-editor-title").textContent = "Nueva consulta";
    el(".me-parent").hidden = true;
    render();
    el(".me-entity").value = preset.entity || "";
    el(".me-query").value = preset.query ?? defaultQuery(form.tool);
    el(".me-purpose").value = preset.purpose || "";
    show();
  }

  async function openFrom(qid) {
    const r = await fetch(`/api/queries/${encodeURIComponent(qid)}/args`);
    if (!r.ok) return notice("No se pudo abrir la consulta", await errorText(r), "fail");
    const data = await r.json();
    await loadOptions();
    const svc = SVC_OF_TOOL[data.tool];
    if (!svc || !source(svc)) return notice("No se puede editar", `${data.tool} no está disponible en el editor.`, "warn");
    const a = data.args || {};
    form = { svc, env: a.env || source(svc).envs[0]?.name, tool: data.tool, runMode: "edit", parent: qid,
             sessionId: data.session_id };
    el("#manual-editor-title").textContent = "Editar y ejecutar";
    const parent = el(".me-parent");
    parent.hidden = false;
    parent.textContent = `basada en #${qid.slice(0, 6)}`;
    render();
    el(".me-entity").value = a.index || a.log_group || "";
    el(".me-query").value = queryText(data.tool, a);
    if (svc === "db") el(".me-limit").value = a.limit || "";
    if (svc === "cw") {
      el(".me-start").value = a.start || "-1h";
      el(".me-end").value = a.end || "now";
    }
    el(".me-purpose").value = data.purpose || "";
    show();
  }

  function show() {
    last = null;
    el(".me-confirm input").checked = false;
    if (!dlg.open) dlg.showModal();
    window.manualGuide?.refresh?.(form);
    scheduleCheck(0);
    setTimeout(() => (form.runMode === "edit" ? el(".me-query") : el(".me-entity")).focus(), 30);
  }

  // --- formulario ----------------------------------------------------------------------------------------------

  function seg(container, items, current, onPick) {
    container.replaceChildren(...items.map((it) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "me-opt";
      b.setAttribute("aria-pressed", String(it.value === current));
      if (it.tone) b.dataset.tone = it.tone;
      if (it.icon) b.innerHTML = `<span class="svc-icon" aria-hidden="true">${ICONS[it.icon]}</span>`;
      b.append(document.createTextNode(it.label));
      if (it.tip) b.dataset.tip = it.tip;
      b.addEventListener("click", () => onPick(it.value));
      return b;
    }));
  }

  function render() {
    const src = source(form.svc);
    seg(el(".me-sources"), sources().map((s) => ({ value: s.svc, label: s.label, icon: s.svc })), form.svc, (svc) => {
      if (svc === form.svc) return;
      form.svc = svc;
      form.env = source(svc).envs[0]?.name;
      form.tool = TOOLS[svc][0].tool;
      el(".me-entity").value = "";
      el(".me-query").value = defaultQuery(form.tool);
      render();
      window.manualGuide?.refresh?.(form);
      scheduleCheck(0);
    });
    seg(el(".me-envs"), src.envs.map((e) => ({ value: e.name, label: e.name.toUpperCase(), tone: envTone(e.name),
      tip: [e.description, e.hint].filter(Boolean).join(" · ") })), form.env, (env) => {
      form.env = env;
      render();
      window.manualGuide?.refresh?.(form);
      scheduleCheck(0);
    });
    const envInfo = src.envs.find((e) => e.name === form.env);
    el(".me-env-hint").textContent = [envInfo?.description, envInfo?.hint].filter(Boolean).join(" · ");
    el(".me-env-hint").hidden = !el(".me-env-hint").textContent;
    el(".me-prod-warn").hidden = envTone(form.env || "") !== "prod";
    seg(el(".me-tools"), TOOLS[form.svc].map((t) => ({ value: t.tool, label: t.label })), form.tool, (tool) => {
      if (tool === form.tool) return;
      const text = el(".me-query").value;
      if (form.svc === "os") el(".me-query").value = convertOs(text, tool);
      form.tool = tool;
      render();
      scheduleCheck(0);
    });
    el(".me-tools-step").hidden = TOOLS[form.svc].length < 2;
    const ent = ENTITY[form.svc];
    el(".me-entity-label").textContent = ent.label;
    el(".me-entity").placeholder = ent.placeholder;
    el(".me-entity-hint").textContent = ent.hint;
    el(".me-query-label").textContent = { os_search: "Body de la búsqueda (JSON)", os_count: "Filtro (JSON)",
      db_select: "SQL", cw_query: "Consulta de Logs Insights" }[form.tool];
    el(".me-query").placeholder = defaultQuery(form.tool);
    el(".me-db-extra").hidden = form.svc !== "db";
    el(".me-cw-extra").hidden = form.svc !== "cw";
    el(".me-count").hidden = form.svc !== "os";
    if (form.svc === "db") el(".me-limit").placeholder = String(opts.limits.db_default_limit);
    if (form.svc === "cw" && !el(".me-start").value) {
      el(".me-start").value = "-1h";
      el(".me-end").value = "now";
    }
    el(".me-num-entity").textContent = el(".me-tools-step").hidden ? "3" : "4";
    el(".me-num-query").textContent = el(".me-tools-step").hidden ? "4" : "5";
    el(".me-num-purpose").textContent = el(".me-tools-step").hidden ? "5" : "6";
  }

  function defaultQuery(tool) {
    return {
      os_search: '{\n  "query": { "match_all": {} },\n  "_source": [],\n  "size": 10\n}',
      os_count: '{\n  "query": { "match_all": {} }\n}',
      db_select: "SELECT *\nFROM tabla\nWHERE id = 1",
      cw_query: "fields @timestamp, @message\n| filter @message like /texto/\n| sort @timestamp desc\n| limit 50",
    }[tool];
  }

  function queryText(tool, a) {
    if (tool === "db_select") return a.sql || "";
    if (tool === "cw_query") return a.query || "";
    if (tool === "os_count") return JSON.stringify(a.query ? { query: a.query } : {}, null, 2);
    const body = {};
    for (const [key, arg] of Object.entries(OS_KEYS)) if (a[arg] !== undefined) body[key] = a[arg];
    return JSON.stringify(body, null, 2);
  }

  // Al cambiar Buscar ↔ Contar se conserva el filtro.
  function convertOs(text, tool) {
    try {
      const body = JSON.parse(text || "{}");
      return tool === "os_count" ? JSON.stringify(body.query ? { query: body.query } : {}, null, 2)
        : JSON.stringify({ query: body.query || { match_all: {} }, size: 10 }, null, 2);
    } catch {
      return text;
    }
  }

  // Los argumentos de la tool a partir del formulario; lanza un Error con un texto para el ingeniero.
  function buildArgs() {
    const a = { env: form.env };
    const entity = el(".me-entity").value.trim();
    const text = el(".me-query").value;
    if (form.svc === "os") {
      a.index = entity;
      let body;
      try {
        body = text.trim() ? JSON.parse(text) : {};
      } catch (e) {
        throw new Error(`el JSON no es válido: ${e.message}`);
      }
      if (typeof body !== "object" || Array.isArray(body) || body === null) throw new Error("el body tiene que ser un objeto JSON");
      if (form.tool === "os_count") {
        const extra = Object.keys(body).filter((k) => k !== "query");
        if (extra.length) throw new Error(`para contar solo va "query" (sobra ${extra.join(", ")})`);
        if (body.query) a.query = body.query;
      } else {
        const extra = Object.keys(body).filter((k) => !(k in OS_KEYS));
        if (extra.length) throw new Error(`claves no admitidas: ${extra.join(", ")} (usa ${Object.keys(OS_KEYS).join(", ")})`);
        for (const [key, arg] of Object.entries(OS_KEYS)) {
          if (body[key] === undefined) continue;
          if (key === "_source" && Array.isArray(body[key]) && !body[key].length) continue;  // [] = todos los campos
          a[arg] = body[key];
        }
      }
    } else if (form.svc === "db") {
      a.sql = text;
      const limit = el(".me-limit").value.trim();
      if (limit) a.limit = Number(limit);
    } else {
      a.log_group = entity;
      a.query = text;
      a.start = el(".me-start").value.trim() || "-1h";
      a.end = el(".me-end").value.trim() || "now";
      if (last?.ok && last.needs_confirmation && el(".me-confirm input").checked) a.max_cost_usd = last.confirm_max_cost_usd;
    }
    return a;
  }

  // --- validación en seco --------------------------------------------------------------------------------------

  function scheduleCheck(delay) {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(check, delay ?? (form?.svc === "cw" ? 800 : 400));
  }

  async function check(count = false) {
    if (!form || !dlg.open) return;
    const seq = ++checkSeq;
    const box = el(".me-check");
    let args;
    try {
      args = buildArgs();
    } catch (e) {
      last = { ok: false, error: e.message };
      paintCheck();
      return;
    }
    if ((form.svc === "os" && !args.index) || (form.svc === "cw" && !args.log_group)) {
      last = { ok: false, error: `falta el ${form.svc === "os" ? "índice" : "log group"}`, pending: true };
      paintCheck();
      return;
    }
    box.dataset.state = "checking";
    const r = await action("/api/manual/validate", { tool: form.tool, args, count });
    if (seq !== checkSeq) return;
    last = r.ok ? await r.json() : { ok: false, error: await errorText(r) };
    paintCheck();
    if (count && last.ok && last.count != null) {
      el(".me-count-result").textContent = `${Number(last.count).toLocaleString()} documentos coinciden`;
    }
  }

  function paintCheck() {
    const box = el(".me-check");
    const confirm = el(".me-confirm");
    box.dataset.state = last?.ok ? "ok" : last?.pending ? "pending" : "error";
    const lines = [];
    if (!last) lines.push(["muted", "Revisando…"]);
    else if (!last.ok) lines.push([last.pending ? "muted" : "fail", last.pending ? `Falta: ${last.error.replace(/^falta el /, "")}` : last.error]);
    else {
      lines.push(["ok", "Lista para correr con los mismos guards que el agente"]);
      if (last.note) lines.push(["muted", last.note]);
      if (last.estimate) {
        const e = last.estimate;
        lines.push([last.needs_confirmation ? "warn" : "muted",
          `Estimado ${e.gb.toFixed(2)} GB · $${e.cost_usd.toFixed(4)} (con margen $${e.with_margin_usd.toFixed(4)}) · tope $${last.cap_usd}`]);
        if (last.window) lines.push(["muted", `Ventana: ${last.window[0]} → ${last.window[1]}`]);
        if (last.blocked) lines.push(["fail", last.blocked]);
      }
    }
    box.replaceChildren(...lines.map(([cls, text]) => {
      const p = document.createElement("p");
      p.className = cls;
      p.textContent = text;
      return p;
    }));
    if (last?.ok && last.final) {
      const det = document.createElement("details");
      det.innerHTML = "<summary>Consulta final</summary><pre></pre>";
      det.lastChild.textContent = last.final;
      box.append(det);
    }
    confirm.hidden = !(last?.ok && last.needs_confirmation && !last.blocked);
    if (!confirm.hidden) {
      $("span", confirm).textContent = `Acepto el costo estimado (hasta $${last.confirm_max_cost_usd}) y la corro sin pedir aprobación`;
    }
    updateRun();
  }

  function updateRun() {
    const purpose = el(".me-purpose").value.trim();
    const needs = last?.ok && last.needs_confirmation && !el(".me-confirm input").checked;
    const ready = last?.ok && !last.blocked && purpose && !needs && !running;
    const btn = el(".me-run");
    btn.disabled = !ready;
    btn.dataset.tip = !last?.ok ? "Corrige la consulta primero" : !purpose ? "Escribe para qué es (título de la tarjeta)"
      : needs ? "Acepta el costo estimado" : "";
  }

  // --- correr --------------------------------------------------------------------------------------------------

  async function run() {
    let args;
    try {
      args = buildArgs();
    } catch (e) {
      last = { ok: false, error: e.message };
      return paintCheck();
    }
    running = true;
    updateRun();
    const btn = el(".me-run");
    $("span:last-child", btn).textContent = "Lanzando…";
    const r = await action(`/api/sessions/${form.sessionId}/queries`, {
      tool: form.tool, args, purpose: el(".me-purpose").value.trim(), run_mode: form.runMode,
      parent_query_id: form.parent,
    });
    running = false;
    $("span:last-child", btn).textContent = "Ejecutar";
    if (!r.ok) {
      last = { ok: false, error: await errorText(r) };
      return paintCheck();
    }
    const { query_id } = await r.json();
    dlg.close();
    notice("Consulta lanzada", "Queda en la sesión como tuya; el agente la ve como evidencia.", "ok");
    if (state.cards.has(query_id)) focusCard(query_id);
    else state.focusNext = query_id;
  }

  // --- eventos -------------------------------------------------------------------------------------------------

  if (dlg) {
    fillIcons(dlg);
    el(".modal-close").addEventListener("click", () => dlg.close());
    el(".me-cancel").addEventListener("click", () => dlg.close());
    el(".me-run").addEventListener("click", run);
    el(".me-count").addEventListener("click", () => {
      el(".me-count-result").textContent = "Contando…";
      check(true);
    });
    ["input", "change"].forEach((type) => {
      dlg.addEventListener(type, (e) => {
        if (e.target.matches(".me-purpose")) return updateRun();
        if (e.target.matches(".me-confirm input")) return updateRun();
        if (e.target.matches(".me-entity, .me-query, .me-limit, .me-start, .me-end")) {
          el(".me-count-result").textContent = "";
          if (e.target.matches(".me-entity")) window.manualGuide?.entityChanged?.(form, e.target.value.trim());
          scheduleCheck();
        }
      });
    });
    // Tab en el textarea inserta dos espacios (JSON y SQL); Ctrl/⌘+Enter corre.
    el(".me-query").addEventListener("keydown", (e) => {
      if (e.key === "Tab" && !e.shiftKey) {
        e.preventDefault();
        e.target.setRangeText("  ", e.target.selectionStart, e.target.selectionEnd, "end");
        scheduleCheck();
      }
    });
    dlg.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !el(".me-run").disabled) {
        e.preventDefault();
        run();
      }
    });
  }

  // Para la guía: insertar texto donde está el cursor y reemplazar la consulta.
  function insert(text) {
    const ta = el(".me-query");
    ta.focus();
    ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
    scheduleCheck();
  }

  function setQuery(text, extra = {}) {
    el(".me-query").value = text;
    if (extra.entity != null) el(".me-entity").value = extra.entity;
    if (extra.limit != null && form.svc === "db") el(".me-limit").value = extra.limit;
    if (extra.start != null && form.svc === "cw") el(".me-start").value = extra.start;
    if (extra.end != null && form.svc === "cw") el(".me-end").value = extra.end;
    if (extra.purpose && !el(".me-purpose").value.trim()) el(".me-purpose").value = extra.purpose;
    scheduleCheck(0);
  }

  return { openNew, openFrom, insert, setQuery, queryText, current: () => form, entity: () => el(".me-entity").value.trim() };
})();
window.manualEditor = manualEditor;
