// Visor sin build: sesiones a la izquierda, timeline de la sesión elegida con su resumen, actualizado por SSE.

const $ = (sel, root = document) => root.querySelector(sel);

// Tema elegido en Ajustes (solo este navegador). Sin elección, sigue al sistema.
const THEME_KEY = "casebook-theme";
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
function storedTheme() {
  try {
    return localStorage.getItem(THEME_KEY) || "system";
  } catch {
    return "system";
  }
}
applyTheme(storedTheme());

// Íconos simples (trazo con currentColor) y servicio por prefijo de tool.
const svg = (body) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const ICONS = {
  os: svg('<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>'),
  cw: svg('<path d="M17.5 19a4.5 4.5 0 0 0 .5-8.97A6 6 0 0 0 6.34 9.5 4 4 0 0 0 6.5 19z"/><path d="M9 15h6M9 12.5h4"/>'),
  db: svg('<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>'),
  note: svg('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
  draft: svg('<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h3"/>'),
  health: svg('<path d="M3 12h4l3-7 4 14 3-7h4"/>'),
  sessions: svg('<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>'),
  copy: svg('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>'),
  refresh: svg('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'),
  clock: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  warn: svg('<path d="M12 3 2 21h20z"/><path d="M12 10v4M12 17.5v.5"/>'),
  external: svg('<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v6H4V6h6"/>'),
  download: svg('<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>'),
  settings: svg('<path d="M4 7h16M4 17h16"/><circle cx="15" cy="7" r="2.5" fill="var(--panel)"/><circle cx="9" cy="17" r="2.5" fill="var(--panel)"/>'),
  terminal: svg('<path d="m5 7 5 5-5 5M12 17h7"/>'),
  skills: svg('<path d="M13 2 4 14h8l-1 8 9-12h-8z"/>'),
  link: svg('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
  close: svg('<path d="M6 6l12 12M18 6 6 18"/>'),
  trash: svg('<path d="M4 7h16M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
  slack: svg('<path d="M10 3 8 21M16 3l-2 18M4 8.5h16M3.5 15.5h16"/>'),
  spark: svg('<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>'),
};
const SERVICES = { os: "OpenSearch", cw: "CloudWatch", db: "MySQL" };
const svcOf = (tool) => ({ os_: "os", cw_: "cw", db_: "db" })[tool?.slice(0, 3)] || null;
const emptyStats = () => Object.fromEntries(Object.keys(SERVICES).map((k) => [k, { total: 0, running: 0, error: 0, rejected: 0, cost: 0 }]));

const state = {
  meta: { version: "", jira_base_url: null, systems: [] },
  sessions: [],
  // Origen (todas, cards o Slack) y refinamientos que se combinan con él. Se recuerda en este navegador.
  sessionFilter: { origin: "all", errors: false, week: false },
  sessionPage: 0,
  pagedFor: null,     // sesión cuya página ya se mostró, para saltar a la página de la sesión abierta
  current: null,      // id de la sesión mostrada
  cards: new Map(),   // query_id -> {el, data, svc, sources}
  stats: emptyStats(), // llamadas por servicio en la sesión actual
  lastEventId: 0,     // último evento aplicado de la sesión actual
  lastTs: null,       // hora del último evento de la sesión actual
  waiting: 0,         // consultas esperando aprobación
  notes: [],          // {key, text}
  drafts: new Map(),
  draft: null,        // el borrador más nuevo {key, card, text}
  sources: new Map(), // "svc|nombre" -> {svc, name, total, errors}
  filter: { kind: "all", text: "" },
};

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const hourFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const dayFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });

// "hace 3 min", "ayer 17:05", "10 oct"
function relTime(ts) {
  const d = new Date(ts);
  const secs = (Date.now() - d.getTime()) / 1000;
  if (secs < 60) return "recién";
  if (secs < 3600) return `hace ${Math.floor(secs / 60)} min`;
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return `hoy ${hourFmt.format(d)}`;
  if (d.toDateString() === yesterday.toDateString()) return `ayer ${hourFmt.format(d)}`;
  return dayFmt.format(d);
}

function duration(fromTs, toTs) {
  const s = Math.max(0, Math.round((new Date(toTs) - new Date(fromTs)) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${s % 60} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

// Rellena los <span data-icon> que vienen en el HTML.
function fillIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => (el.innerHTML = ICONS[el.dataset.icon] || ""));
}

// --- sesiones --------------------------------------------------------------------------------------------

async function loadSessions() {
  state.sessions = await fetch("/api/sessions").then((r) => r.json());
  renderSessions();
}

// De dónde viene la sesión: la vigilancia de Slack (tiene eventos de Slack, aunque después le pongan una card),
// una card del equipo (con el prefijo del pack, o cualquiera si no hay), u otra cosa ("Sin identificar"), que solo
// aparece en Todas.
function cardPattern() {
  const prefix = (state.meta.card_prefix || "").replace(/[^A-Za-z0-9]/g, "");
  return new RegExp(prefix ? `^${prefix}-\\d+` : "^[A-Z][A-Z0-9]+-\\d+", "i");
}

function sessionOrigin(s) {
  if (s.slack) return "slack";
  const card = cardPattern();
  if (card.test(s.card || "") || card.test(s.name || "")) return "card";
  return "other";
}

function sessionRefined(s, f = state.sessionFilter) {
  if (f.errors && !(s.errors > 0)) return false;
  if (f.week && Date.now() - new Date(s.last_event_at || s.created_at) >= 7 * 864e5) return false;
  return true;
}

function sessionVisible(s) {
  const { origin } = state.sessionFilter;
  return (origin === "all" || sessionOrigin(s) === origin) && sessionRefined(s);
}

const FILTER_KEY = "casebook-session-filter";
try {
  const saved = JSON.parse(localStorage.getItem(FILTER_KEY) || "null");
  if (saved && ["all", "card", "slack"].includes(saved.origin)) {
    state.sessionFilter = { origin: saved.origin, errors: !!saved.errors, week: !!saved.week };
  }
} catch {
  // sin almacenamiento: queda el filtro por defecto
}

function renderSessionFilters() {
  const f = state.sessionFilter;
  // Cada origen cuenta lo que mostraría con los refinamientos activos.
  const refined = state.sessions.filter((s) => sessionRefined(s));
  const counts = { all: refined.length, card: 0, slack: 0 };
  refined.forEach((s) => {
    const o = sessionOrigin(s);
    if (o in counts) counts[o] += 1;
  });
  document.querySelectorAll(".seg-btn").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.origin === f.origin));
    $(".count", b).textContent = counts[b.dataset.origin];
  });
  document.querySelectorAll(".chip-toggle").forEach((b) => b.setAttribute("aria-pressed", String(!!f[b.dataset.refine])));
}

function setSessionFilter(change) {
  state.sessionFilter = { ...state.sessionFilter, ...change };
  state.sessionPage = 0;
  try {
    localStorage.setItem(FILTER_KEY, JSON.stringify(state.sessionFilter));
  } catch {
    // sin almacenamiento: el filtro vale solo para esta pestaña
  }
  renderSessions();
}

const SESSIONS_PER_PAGE = 20;

function renderSessions() {
  $("#session-count").textContent = state.sessions.length || "";
  renderSessionFilters();
  const nav = $("#sessions");
  // Mismo orden que /api/sessions, también al recibir actividad por SSE.
  state.sessions.sort((a, b) =>
    (b.last_event_at || b.created_at).localeCompare(a.last_event_at || a.created_at) ||
    (b.last_event_id || 0) - (a.last_event_id || 0) || b.id - a.id
  );
  const visible = state.sessions.filter(sessionVisible);
  const pages = Math.max(1, Math.ceil(visible.length / SESSIONS_PER_PAGE));
  if (state.pagedFor !== state.current) {
    const i = visible.findIndex((s) => s.id === state.current);
    if (i >= 0) state.sessionPage = Math.floor(i / SESSIONS_PER_PAGE);
    state.pagedFor = state.current;
  }
  state.sessionPage = Math.min(state.sessionPage, pages - 1);
  const from = state.sessionPage * SESSIONS_PER_PAGE;
  const shown = visible.slice(from, from + SESSIONS_PER_PAGE);
  $("#session-pager").hidden = pages < 2;
  $("#pager-info").textContent = `${from + 1}–${from + shown.length} de ${visible.length}`;
  $("#pager-prev").disabled = state.sessionPage === 0;
  $("#pager-next").disabled = state.sessionPage >= pages - 1;
  if (!shown.length) {
    nav.innerHTML = `<p class="empty">${state.sessions.length ? "Ninguna sesión coincide." : "Sin sesiones."}</p>`;
    return;
  }
  nav.replaceChildren(
    ...shown.map((s) => {
      const a = document.createElement("a");
      a.href = `#/sessions/${s.id}`;
      if (s.id === state.current) a.setAttribute("aria-current", "page");
      a.innerHTML = `<span class="name"><span class="label"></span></span><span class="sub"></span>`;
      $(".label", a).textContent = s.name;
      if (s.errors) {
        const err = document.createElement("span");
        err.className = "err";
        err.textContent = `${s.errors} ✗`;
        err.title = `${s.errors} consulta${s.errors > 1 ? "s" : ""} con error o rechazada${s.errors > 1 ? "s" : ""}`;
        $(".name", a).append(err);
      }
      const card = s.card && s.card !== s.name ? s.card + " · " : "";
      $(".sub", a).textContent = `${card}${s.events} eventos · ${relTime(s.last_event_at || s.created_at)}`;
      a.title = `Abierta ${dateFmt.format(new Date(s.created_at))}`;
      return a;
    })
  );
}

document.querySelectorAll(".seg-btn").forEach((btn) =>
  btn.addEventListener("click", () => setSessionFilter({ origin: btn.dataset.origin }))
);
document.querySelectorAll(".chip-toggle").forEach((btn) =>
  btn.addEventListener("click", () => setSessionFilter({ [btn.dataset.refine]: !state.sessionFilter[btn.dataset.refine] }))
);

function goSessionPage(delta) {
  state.sessionPage += delta;
  renderSessions();
  $("#sessions").scrollTop = 0;
}
$("#pager-prev").addEventListener("click", () => goSessionPage(-1));
$("#pager-next").addEventListener("click", () => goSessionPage(1));

function sessionFromHash() {
  const m = location.hash.match(/^#\/sessions\/(\d+)/);
  return m ? Number(m[1]) : null;
}

// #/sessions/12/<clave> apunta a una tarjeta: el query_id de una consulta o "e<id>" de una nota o borrador.
function cardKeyFromHash() {
  const m = location.hash.match(/^#\/sessions\/\d+\/([\w-]+)/);
  return m ? m[1] : null;
}

function focusCard(key) {
  if (!key) return;
  const grouped = [...state.drafts.values()].find((d) => d.versions.some((v) => `e${v.id}` === key));
  const el = grouped?.el || document.querySelector(`#timeline [data-key="${CSS.escape(key)}"]`);
  if (!el) return;
  if (el.hidden) setKindFilter("all");
  el.scrollIntoView({ block: "center" });
  el.classList.add("highlight");
  setTimeout(() => el.classList.remove("highlight"), 2500);
}

// Los query_id que Claude cita en una nota se muestran como una pill (servicio, tool y hash corto) que abre la
// tarjeta en un modal, sin mover el scroll de la nota. Solo se enlazan los de consultas de esta sesión, que ya
// están en el timeline porque la nota llega después de la consulta. Se come la "q " que Claude pone delante.
// Un error de la vigilancia de Slack: título corto, qué hacer y el detalle técnico plegado. Acepta el formato viejo
// (un texto) para estados y eventos guardados antes. `retry` agrega el botón «Reintentar».
function slackErrorInfo(err) {
  if (!err) return null;
  if (typeof err === "string") return { code: "other", title: "No se pudo leer Slack", hint: "", detail: err, count: 1 };
  return { count: 1, hint: "", detail: "", ...err };
}

function renderSlackError(err, { retry } = {}) {
  const e = slackErrorInfo(err);
  const box = document.createElement("div");
  box.className = "slack-alert";
  box.setAttribute("role", "alert");
  box.dataset.key = `${e.code}|${e.at}|${e.count}`;
  box.innerHTML = `<div class="slack-alert-head"><span class="slack-alert-icon" aria-hidden="true">${ICONS.warn}</span>
    <strong></strong><span class="slack-alert-count"></span></div>`;
  $("strong", box).textContent = e.title;
  const count = $(".slack-alert-count", box);
  count.textContent = e.count > 1 ? `×${e.count}` : "";
  count.title = e.count > 1 ? `Pasó ${e.count} veces seguidas` : "";
  if (e.hint) {
    const hint = document.createElement("p");
    hint.className = "slack-alert-hint";
    hint.textContent = e.hint;
    box.append(hint);
  }
  if (e.detail) {
    const details = document.createElement("details");
    details.innerHTML = "<summary>Ver detalle</summary><pre></pre>";
    $("pre", details).textContent = e.detail;
    box.append(details);
  }
  if (retry || e.at) {
    const actions = document.createElement("div");
    actions.className = "slack-alert-actions";
    if (e.at) actions.append(Object.assign(document.createElement("span"), { textContent: relTime(e.at * 1000) }));
    if (retry) {
      const b = document.createElement("button");
      b.className = "ghost";
      b.innerHTML = `<span class="btn-icon" aria-hidden="true">${ICONS.refresh}</span><span>Reintentar</span>`;
      b.addEventListener("click", () => retry(b));
      actions.append(b);
    }
    box.append(actions);
  }
  return box;
}

// Lo que deja la vigilancia de Slack. El mensaje del canal (y una repregunta) es la entrada del caso y va en el
// filtro Slack; el análisis es la conclusión, como una nota de Claude, y va en Notas; un análisis que falló o quedó
// sin hacer por el tope sigue en Slack.
const SLACK_TITLES = { analysis: "Conclusión del análisis de Slack", skipped: "Sin analizar", error: "El análisis automático falló" };

function slackCard(ev, p) {
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const analysis = p.kind === "analysis";
  const el = node("li", `card ${analysis ? "note" : "slack"} slack-${p.kind}`);
  el.dataset.key = `e${ev.id}`;
  el.dataset.kind = analysis ? "note" : "slack";
  if (p.kind === "error") el.dataset.error = "1";
  el.innerHTML = `<div class="card-head"><span class="svc-icon" aria-hidden="true">${analysis ? ICONS.note : ICONS.slack}</span>
    <span class="purpose"></span><time></time></div>`;
  $("time", el).textContent = timeFmt.format(new Date(ev.ts));
  const purpose = $(".purpose", el);
  const body = node("div", "slack-body");
  el.append(body);

  if (p.kind === "message") {
    const grouped = (p.covers?.length || 0) > 1;
    purpose.textContent = grouped ? `Respuestas nuevas en el hilo (${p.covers.length})`
      : `${p.follow_up ? "Repregunta" : "Mensaje"} de ${p.author} en Slack`;
    body.append(node("blockquote", "slack-quote", p.text));
    if (p.files?.length) {
      const files = node("div", "slack-files");
      p.files.forEach((f) => {
        const chip = node("span", "slack-file", f.name || f.id);
        chip.title = [f.id, f.mimetype].filter(Boolean).join(" · ");
        files.append(chip);
      });
      body.append(files);
    }
    return el;
  }
  if (p.error) {
    purpose.textContent = SLACK_TITLES[p.kind] || "Slack";
    body.append(renderSlackError(p.error));
    return el;
  }
  if (p.kind !== "analysis" || !p.result) {
    purpose.textContent = SLACK_TITLES[p.kind] || "Slack";
    body.append(node("p", "note-text", p.text));
    return el;
  }
  const r = p.result;
  purpose.textContent = SLACK_TITLES.analysis;
  const escalation = !!p.escalation;
  const badge = node("span", `slack-outcome outcome-${p.outcome}${escalation ? " escalation" : ""}`, p.outcome_label || p.outcome);
  purpose.after(badge);
  if (r.category) body.append(node("p", "slack-category", r.category));
  if (r.summary) body.append(node("p", "slack-summary", r.summary));
  const field = (label, text) => {
    if (!text) return;
    const row = node("p", "slack-field");
    row.append(node("strong", null, `${label}: `), text);
    body.append(row);
  };
  field("Hipótesis", r.hypothesis);
  field("Confianza", { high: "alta", medium: "media", low: "baja" }[r.confidence] || r.confidence);
  field("Falta", r.missing_data);
  const list = (label, items, cls) => {
    if (!items?.length) return null;
    body.append(node("p", "slack-label", label));
    const ul = node("ul", cls);
    body.append(ul);
    return ul;
  };
  const evidence = list("Evidencia", r.evidence, "slack-steps");
  r.evidence?.forEach((e) => {
    const li = node("li");
    linkQueryIds(li, e?.query_id ? `${e.finding} (q ${e.query_id})` : String(e?.finding || ""));
    if (e?.ref) li.append(" ", node("code", "slack-ref", e.ref));
    evidence.append(li);
  });
  const discarded = list("Descartado", r.discarded, "slack-steps");
  r.discarded?.forEach((d) => discarded.append(node("li", null, d)));
  const similar = list("Cards similares", r.similar_cards, "slack-steps");
  r.similar_cards?.forEach((c) => similar.append(node("li", null, c)));
  if (r.steps?.length) {
    body.append(node("p", "slack-label", "Pasos"));
    const ol = node("ol", "slack-steps");
    r.steps.forEach((s) => ol.append(node("li", null, s)));
    body.append(ol);
  }
  // Los análisis guardados con una versión anterior traían la card en un campo «<algo>_card».
  const card = r.card || Object.entries(r).find(([k, v]) => k.endsWith("_card") && typeof v === "string")?.[1];
  if (card) {
    body.append(node("p", "slack-label", p.card_label || "Card"));
    body.append(node("p", "slack-card", card));
  }
  if (r.attachments_reviewed?.length) {
    body.append(node("p", "slack-label", `Adjuntos revisados (${r.attachments_reviewed.length})`));
    const ul = node("ul", "slack-attachments");
    r.attachments_reviewed.forEach((a) => {
      const li = node("li");
      li.append(node("code", null, a.file_id), ` ${a.description}`);
      ul.append(li);
    });
    body.append(ul);
  }
  if (r.attachments_missing?.length) {
    body.append(node("p", "slack-warning", `El análisis no dice haber revisado ${r.attachments_missing.length === 1
      ? "un adjunto" : `${r.attachments_missing.length} adjuntos`} del mensaje (${r.attachments_missing.join(", ")}): `
      + "tómalo con cuidado o reintenta."));
  }
  if (r.suggested_reply) {
    const reply = node("div", "slack-reply");
    const head = node("div", "slack-reply-head");
    head.append(node("span", "slack-label", "Respuesta sugerida"));
    const copy = node("button", "ghost");
    copy.innerHTML = `<span class="btn-icon" aria-hidden="true">${ICONS.copy}</span><span>Copiar</span>`;
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(r.suggested_reply);
      flash(copy, "Copiado");
    });
    head.append(copy);
    reply.append(head, node("pre", null, r.suggested_reply));
    body.append(reply);
  }
  return el;
}

function linkQueryIds(el, text) {
  const parts = [];
  let last = 0;
  for (const m of text.matchAll(/\b(?:q\s+)?([0-9a-f]{12})\b/g)) {
    const id = m[1];
    const card = state.cards.get(id);
    if (!card) continue;
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = `query-ref${card.svc ? ` svc-${card.svc}` : ""}`;
    pill.title = `${$(".purpose", card.el).textContent} · ${id}`;
    pill.innerHTML = `<span class="svc-icon" aria-hidden="true">${ICONS[card.svc] || ""}</span><span class="ref-tool"></span><span class="ref-id"></span>`;
    $(".ref-tool", pill).textContent = $(".tool", card.el).textContent;
    $(".ref-id", pill).textContent = id.slice(0, 7);
    pill.addEventListener("click", () => openCardModal(id));
    parts.push(text.slice(last, m.index), pill);
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  el.replaceChildren(...parts);
}

// El modal muestra la tarjeta misma (no una copia): sigue recibiendo eventos en vivo y sus tablas conservan
// filtros y orden. Una copia inerte conserva el aspecto del timeline hasta cerrar.
const cardModal = $("#card-modal");
let modalSlot = null;

function openCardModal(id) {
  const card = state.cards.get(id);
  if (!card) return;
  if (cardModal.open) closeCardModal();
  modalSlot = card.el.cloneNode(true);
  modalSlot.classList.add("modal-slot");
  modalSlot.inert = true;
  modalSlot.setAttribute("aria-hidden", "true");
  modalSlot.removeAttribute("id");
  modalSlot.querySelectorAll("[id]").forEach((el) => el.removeAttribute("id"));
  // Mismo alto que la tarjeta para que el timeline de atrás no se mueva.
  modalSlot.style.height = `${card.el.offsetHeight}px`;
  modalSlot.hidden = card.el.hidden;
  card.el.replaceWith(modalSlot);
  card.el.hidden = false;
  $(".modal-body", cardModal).replaceChildren(card.el);
  cardModal.dataset.key = id;
  cardModal.showModal();
}

function closeCardModal() {
  const el = $(".modal-body > .card", cardModal);
  if (el && modalSlot) modalSlot.replaceWith(el);
  modalSlot = null;
  if (cardModal.open) cardModal.close();
  applyFilters();
}

cardModal.addEventListener("close", closeCardModal);
// Clic en el fondo (fuera del contenido) cierra.
cardModal.addEventListener("click", (e) => e.target === cardModal && cardModal.close());
$(".modal-close", cardModal).addEventListener("click", () => cardModal.close());
$(".modal-goto", cardModal).addEventListener("click", () => {
  // El evento close llega después: la tarjeta tiene que volver al timeline antes de buscarla.
  closeCardModal();
  focusCard(cardModal.dataset.key);
});

const draftHistory = $("#draft-history-modal");
function renderDraftHistory(draft) {
  $("#draft-history-title").textContent = `Versiones del borrador · ${draft.card}`;
  $(".modal-body", draftHistory).replaceChildren(...draft.versions.toReversed().map((ev, index) => {
    const section = document.createElement("section");
    section.className = "draft-version";
    const heading = document.createElement("h3");
    heading.textContent = `Versión ${draft.versions.length - index}${index === 0 ? " · Actual" : ""} · ${new Date(ev.ts).toLocaleString()}`;
    const text = document.createElement("p");
    text.className = "note-text";
    text.textContent = ev.payload.text;
    section.append(heading, text);
    return section;
  }));
}
function openDraftHistory(draft) {
  draftHistory.dataset.card = draft.card;
  renderDraftHistory(draft);
  draftHistory.showModal();
}
$(".modal-close", draftHistory).addEventListener("click", () => draftHistory.close());
draftHistory.addEventListener("click", (e) => e.target === draftHistory && draftHistory.close());

// Las acciones que cambian algo llevan un header propio (el servidor rechaza las que no lo traen).
function action(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "x-casebook-viewer": "1", "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function resetSessionState() {
  state.cards.clear();
  state.lastEventId = 0;
  state.lastTs = null;
  state.waiting = 0;
  state.stats = emptyStats();
  state.notes = [];
  state.draft = null;
  state.drafts.clear();
  draftHistory.close();
  state.sources = new Map();
  state.filter = { kind: "all", text: "" };
}

async function openSession(id) {
  if (cardModal.open) cardModal.close();
  state.current = id;
  resetSessionState();
  $("#timeline").replaceChildren();
  renderSessions();
  renderSummary();

  const session = state.sessions.find((s) => s.id === id);
  const head = $("#session-head");
  if (!session) {
    head.innerHTML = `<p class="empty">Todavía no hay sesiones. Pídele a Claude que investigue un caso, por ejemplo: «Investiga <span class="card-example"></span>». Aquí verás las consultas y los hallazgos.</p>`;
    $(".card-example", head).textContent = state.meta.card_example || "CASO-1234";
    $("#filtered-empty").hidden = true;
    return;
  }
  head.innerHTML = `<div class="head-row">
      <div class="titles"><h2><span class="session-id" tabindex="0"></span><span class="title"></span><span class="badge running" hidden></span></h2><p></p></div>
      <div class="session-actions">
        <button class="thread-check" hidden><span class="btn-icon" data-icon="slack"></span><span class="label">Revisar respuestas del hilo</span></button>
        <a class="btn open-card" target="_blank" rel="noopener" hidden><span class="btn-icon" data-icon="external"></span>Abrir card</a>
        <button class="copy-md"><span class="btn-icon" data-icon="copy"></span><span>Copiar Markdown</span></button>
        <a class="btn" href="/api/sessions/${id}/export.md?download=true"><span class="btn-icon" data-icon="download"></span>Descargar .md</a>
      </div>
    </div>
    <div class="thread-status" aria-live="polite" hidden></div>
    <div class="stats" aria-label="Llamadas por servicio"></div>
    <div class="card-filters" role="group" aria-label="Filtrar tarjetas"></div>`;
  fillIcons(head);
  $(".copy-md", head).addEventListener("click", async (e) => {
    const md = await fetch(`/api/sessions/${id}/export.md`).then((r) => r.text());
    await navigator.clipboard.writeText(md);
    flash(e.currentTarget, "Copiado");
  });
  if (state.meta.jira_base_url && session.card) {
    const link = $(".open-card", head);
    link.href = state.meta.jira_base_url + encodeURIComponent(session.card);
    link.hidden = false;
  }
  $(".title", head).textContent = session.name;
  $(".thread-check", head).addEventListener("click", () => threadCheck.start(id));
  renderFilters();
  renderHead();

  const events = await fetch(`/api/sessions/${id}/events`).then((r) => r.json());
  if (state.current !== id) return;
  events.forEach((ev) => applyEvent(ev, false));
  renderHead();
  renderSummary();
  focusCard(cardKeyFromHash());
  threadCheck.load(id);
}

// «Revisar respuestas del hilo» en una sesión que viene de Slack: lee lo nuevo del hilo y, si hay algo, lo analiza
// en esta misma sesión (las tarjetas llegan por SSE como cualquier evento). Aquí solo el botón y su estado.
const threadCheck = (() => {
  const BUSY = ["searching", "queued"];
  let timer = null;

  async function load(id) {
    clearTimeout(timer);
    const body = await fetch(`/api/slack/thread/${id}`).then((r) => (r.ok ? r.json() : null), () => null);
    if (state.current !== id) return;
    const head = $("#session-head");
    const btn = $(".thread-check", head);
    if (!btn) return;
    btn.hidden = !body?.has_thread;
    render(head, body?.check);
    if (body?.check && BUSY.includes(body.check.status)) timer = setTimeout(() => load(id), 2000);
  }

  async function start(id, force = false) {
    const head = $("#session-head");
    render(head, { status: "searching", at: Date.now() / 1000 });
    const r = await action(`/api/slack/thread/${id}/check${force ? "?force=true" : ""}`);
    if (!r.ok && r.status !== 409) {
      const body = await r.json().catch(() => ({}));
      render(head, { status: "error", at: Date.now() / 1000, error: { code: "other", title: "No se pudo revisar el hilo",
        hint: "", detail: typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`, at: Date.now() / 1000 } });
      return;
    }
    load(id);
  }

  function render(head, check) {
    const btn = $(".thread-check", head);
    const box = $(".thread-status", head);
    if (!btn || !box) return;
    const busy = !!check && BUSY.includes(check.status);
    btn.disabled = busy;
    btn.classList.toggle("busy", busy);
    $(".btn-icon", btn).innerHTML = busy ? ICONS.refresh : ICONS.slack;
    $(".label", btn).textContent = busy ? "Revisando el hilo…" : "Revisar respuestas del hilo";
    // El resultado se muestra un rato; las tarjetas nuevas quedan en el timeline.
    if (!check || (!busy && Date.now() / 1000 - check.at > 600)) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.className = "thread-status";
    if (check.status === "error") {
      box.replaceChildren(renderSlackError(check.error));
      return;
    }
    const n = check.count || 0;
    if (check.status === "no_requests") {
      const who = (check.authors || []).join(", ");
      box.replaceChildren(`${n === 1 ? "Hay 1 respuesta nueva" : `Hay ${n} respuestas nuevas`}${who ? ` (${who})` : ""}, `
        + "pero solo agradecen o confirman: no lo analicé. ");
      const again = Object.assign(document.createElement("button"), { className: "link-btn", textContent: "Analizar igual" });
      again.addEventListener("click", () => start(state.current, true));
      box.append(again);
      return;
    }
    const text = {
      searching: "Leyendo las respuestas del hilo…",
      queued: `Analizando ${n === 1 ? "1 respuesta nueva" : `${n} respuestas nuevas`} del hilo en esta sesión…`,
      empty: "No hay respuestas nuevas en el hilo desde el último análisis.",
      existing: check.busy ? "Las respuestas nuevas del hilo ya se están analizando." : "Las respuestas del hilo ya estaban analizadas.",
      done: "Listo: el análisis de las respuestas del hilo quedó en esta sesión.",
      failed: "El análisis de las respuestas falló: el detalle está en el timeline. Puedes volver a revisar el hilo.",
      skipped: "Sin analizar: se alcanzó el tope de análisis del día.",
    }[check.status] || "";
    box.replaceChildren(text);
    if (!busy) box.append(Object.assign(document.createElement("span"), { className: "when", textContent: ` · ${relTime(check.at * 1000)}` }));
  }

  return { load, start };
})();

// --- cabecera: título, contadores y filtros ---------------------------------------------------------------

function currentSession() {
  return state.sessions.find((s) => s.id === state.current);
}

function renderHead() {
  const head = $("#session-head");
  const session = currentSession();
  if (!session || !$(".stats", head)) return;

  $(".title", head).textContent = session.name;
  // El número sirve para decirle al agente (en el chat) sobre qué sesión trabajar.
  const sid = $(".session-id", head);
  sid.textContent = `#${session.id}`;
  sid.title = `Número de la sesión. Úsalo para pedirle al agente que trabaje sobre esta sesión en concreto, `
    + `por ejemplo: «re-analiza la sesión ${session.id} de casebook más a profundidad».`;
  sid.setAttribute("aria-label", `Sesión ${session.id}. ${sid.title}`);
  const jiraLink = $(".open-card", head);
  jiraLink.hidden = !(state.meta.jira_base_url && session.card);
  if (!jiraLink.hidden) jiraLink.href = state.meta.jira_base_url + encodeURIComponent(session.card);
  const running = Object.values(state.stats).reduce((n, st) => n + st.running, 0);
  const badge = $(".badge", head);
  badge.hidden = !running;
  badge.textContent = state.waiting ? "esperando aprobación" : `${running} en curso`;

  const card = session.card && session.card !== session.name ? `${session.card} · ` : "";
  const last = state.lastTs ? ` · última actividad ${relTime(state.lastTs)}` : "";
  $(".titles p", head).textContent = `${card}abierta ${dateFmt.format(new Date(session.created_at))}${last}`;

  const tiles = Object.entries(SERVICES).map(([svc, name]) => {
    const st = state.stats[svc];
    const parts = [`${st.total} ${st.total === 1 ? "consulta" : "consultas"}`];
    if (svc === "cw" && st.cost) parts.push(`$${st.cost.toFixed(4)}`);
    let value = esc(parts.join(" · "));
    if (st.running) value += ` · <span class="running">${st.running} en curso</span>`;
    const bad = st.error + st.rejected;
    if (bad) value += ` · <span class="bad">${bad} ${bad === 1 ? "error" : "errores"}</span>`;
    return statTile(svc, name, value, !st.total);
  });
  const dur = state.lastTs ? duration(session.created_at, state.lastTs) : "—";
  tiles.push(statTile("clock", "Duración", esc(dur), false));
  $(".stats", head).replaceChildren(...tiles);
  updateFilterCounts();
  sizeHead();
}

function statTile(icon, label, valueHtml, idle) {
  const el = document.createElement("div");
  el.className = `stat${idle ? " idle" : ""}`;
  el.innerHTML = `<span class="svc-icon" aria-hidden="true">${ICONS[icon]}</span>
    <span><span class="stat-label"></span><span class="stat-value">${valueHtml}</span></span>`;
  $(".stat-label", el).textContent = label;
  return el;
}

const KINDS = [
  { kind: "all", label: "Todo" },
  { kind: "os", label: "OpenSearch", icon: "os" },
  { kind: "cw", label: "CloudWatch", icon: "cw" },
  { kind: "db", label: "MySQL", icon: "db" },
  { kind: "slack", label: "Slack", icon: "slack" },
  { kind: "note", label: "Notas", icon: "note" },
  { kind: "err", label: "Errores", icon: "warn" },
];

function renderFilters() {
  const box = $("#session-head .card-filters");
  box.replaceChildren(
    ...KINDS.map(({ kind, label, icon }) => {
      const b = document.createElement("button");
      b.className = `chip-filter svc-${kind}`;
      b.dataset.kind = kind;
      b.setAttribute("aria-pressed", String(state.filter.kind === kind));
      b.innerHTML = `${icon ? `<span class="svc-icon" aria-hidden="true">${ICONS[icon]}</span>` : ""}<span></span> <span class="count"></span>`;
      b.children[icon ? 1 : 0].textContent = label;
      b.addEventListener("click", () => setKindFilter(state.filter.kind === kind ? "all" : kind));
      return b;
    })
  );
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  const input = document.createElement("input");
  input.type = "search";
  input.className = "card-search";
  input.placeholder = "Buscar en esta sesión";
  input.setAttribute("aria-label", "Buscar en esta sesión");
  input.addEventListener("input", () => {
    state.filter.text = input.value.trim().toLowerCase();
    applyFilters();
  });
  box.append(spacer, input);
  updateFilterCounts();
}

function setKindFilter(kind) {
  state.filter.kind = kind;
  document.querySelectorAll(".chip-filter").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.kind === kind)));
  applyFilters();
}

function updateFilterCounts() {
  const cards = [...$("#timeline").children];
  const count = (pred) => cards.filter(pred).length;
  const counts = {
    all: cards.length,
    os: count((c) => c.dataset.kind === "os"),
    cw: count((c) => c.dataset.kind === "cw"),
    db: count((c) => c.dataset.kind === "db"),
    slack: count((c) => c.dataset.kind === "slack"),
    note: count((c) => c.dataset.kind === "note"),
    err: count((c) => c.dataset.error === "1"),
  };
  document.querySelectorAll(".chip-filter").forEach((b) => {
    const n = counts[b.dataset.kind];
    $(".count", b).textContent = n;
    b.disabled = !n && b.dataset.kind !== "all" && state.filter.kind !== b.dataset.kind;
  });
}

function cardMatches(el) {
  const { kind, text } = state.filter;
  if (kind === "err" && el.dataset.error !== "1") return false;
  if (kind !== "all" && kind !== "err" && el.dataset.kind !== kind) return false;
  return !text || el.textContent.toLowerCase().includes(text);
}

function applyFilters() {
  const cards = [...$("#timeline").children];
  cards.forEach((el) => (el.hidden = !cardMatches(el)));
  $("#filtered-empty").hidden = !cards.length || cards.some((el) => !el.hidden);
}

// La columna de resumen queda pegada debajo de la cabecera fija.
function sizeHead() {
  document.documentElement.style.setProperty("--head-h", `${$("#session-head").offsetHeight + 16}px`);
}
new ResizeObserver(sizeHead).observe($("#session-head"));

// --- resumen ---------------------------------------------------------------------------------------------

// De dónde leyó una consulta: índice, log group o tablas. Para db_select se sacan las tablas del SQL.
// La consulta trae params.env: va en el nombre (salvo «default», el de una fuente sin `environment`), así `lead`
// de prod y `lead` de qa no se suman como si fueran la misma tabla.
function sourcesOf(p) {
  const svc = svcOf(p.tool);
  const params = p.params || {};
  const where = (name) => (svc !== "os" && params.env && params.env !== "default" ? `${name} (${params.env})` : name);
  if (svc === "os" && params.index) return [{ svc, name: params.index }];
  if (svc === "cw" && params.log_group) return [{ svc, name: where(params.log_group) }];
  if (p.tool === "db_describe" && params.table) return [{ svc, name: where(params.table) }];
  if (p.tool === "db_select") {
    const sql = p.query || params.sql || "";
    const names = new Set([...sql.matchAll(/\b(?:from|join)\s+`?([\w$.]+)`?/gi)].map((m) => m[1]));
    return [...names].map((name) => ({ svc, name: where(name) }));
  }
  return [];
}

function addSources(list) {
  for (const s of list) {
    const key = `${s.svc}|${s.name}`;
    const entry = state.sources.get(key) || { ...s, total: 0, errors: 0 };
    entry.total += 1;
    state.sources.set(key, entry);
  }
}

function markSourceErrors(list) {
  for (const s of list) {
    const entry = state.sources.get(`${s.svc}|${s.name}`);
    if (entry) entry.errors += 1;
  }
}

function renderSummary() {
  const box = $("#summary");
  const notes = $("#sum-notes");
  notes.hidden = !state.notes.length;
  $(".sum-notes", notes).replaceChildren(
    ...state.notes.map((n) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = `#/sessions/${state.current}/${n.key}`;
      a.textContent = n.text.length > 220 ? n.text.slice(0, 220) + "…" : n.text;
      li.append(a);
      return li;
    })
  );

  const draft = $("#sum-draft");
  draft.hidden = !state.draft;
  if (state.draft) {
    $(".draft-card", draft).textContent = state.draft.card;
    $(".sum-draft-text", draft).textContent = state.draft.text;
  }

  const sources = $("#sum-sources");
  sources.hidden = !state.sources.size;
  $(".sum-sources", sources).replaceChildren(
    ...[...state.sources.values()].map((s) => {
      const li = document.createElement("li");
      li.className = `svc-${s.svc}`;
      li.innerHTML = `<span class="svc-icon" aria-hidden="true">${ICONS[s.svc]}</span><span class="src"></span><span class="n"></span>`;
      $(".src", li).textContent = s.name;
      $(".src", li).title = s.name;
      $(".n", li).innerHTML = s.errors ? `${s.total} · <span class="bad">${s.errors} ✗</span>` : String(s.total);
      return li;
    })
  );
  box.hidden = notes.hidden && draft.hidden && sources.hidden;
}

$("#sum-draft .copy-draft").addEventListener("click", async (e) => {
  if (!state.draft) return;
  await navigator.clipboard.writeText(state.draft.text);
  flash(e.currentTarget, "Copiado");
});
$("#sum-draft .see-draft").addEventListener("click", () => state.draft && focusCard(state.draft.key));

// --- eventos ---------------------------------------------------------------------------------------------

function applyEvent(ev, live = true) {
  if (ev.session_id !== state.current || ev.id <= state.lastEventId) return;
  const add = (el) => prepend(el, live);
  state.lastEventId = ev.id;
  state.lastTs = ev.ts;
  const p = ev.payload;
  let summary = false;

  switch (ev.type) {
    case "note": {
      const el = $("#tpl-note").content.firstElementChild.cloneNode(true);
      el.dataset.key = `e${ev.id}`;
      el.dataset.kind = "note";
      $(".svc-icon", el).innerHTML = ICONS.note;
      $("time", el).textContent = timeFmt.format(new Date(ev.ts));
      linkQueryIds($(".note-text", el), p.text);
      state.notes.push({ key: el.dataset.key, text: p.text });
      summary = true;
      add(el);
      break;
    }
    case "slack": {
      const el = slackCard(ev, p);
      // El análisis es la conclusión de la sesión, como una nota de Claude: va al panel de Conclusiones.
      if (p.kind === "analysis") {
        const r = p.result || {};
        const text = [p.outcome_label, r.summary].filter(Boolean).join(" · ") || p.text;
        state.notes.push({ key: el.dataset.key, text });
        summary = true;
      }
      add(el);
      break;
    }
    case "health": {
      const el = document.createElement("li");
      const failing = p.checks.filter((c) => c.status === "fail").length;
      el.className = `card health${failing ? " has-fail" : ""}`;
      el.dataset.key = `e${ev.id}`;
      el.dataset.kind = "health";
      el.innerHTML = `<div class="card-head"><span class="svc-icon" aria-hidden="true">${ICONS.health}</span>
        <span class="purpose"></span><time></time></div><ul class="checks"></ul>`;
      $(".purpose", el).textContent = failing
        ? `Estado de conexiones: ${failing} con problemas`
        : "Estado de conexiones: todo listo";
      $("time", el).textContent = timeFmt.format(new Date(ev.ts));
      const marks = { ok: "✓", warn: "!", fail: "✗" };
      $(".checks", el).replaceChildren(
        ...p.checks.map((c) => {
          const li = document.createElement("li");
          li.className = `check ${c.status}`;
          li.innerHTML = `<span class="mark"></span><span class="name"></span><span class="detail"></span>`;
          $(".mark", li).textContent = marks[c.status] || "?";
          $(".name", li).textContent = c.name;
          $(".detail", li).textContent = c.detail;
          return li;
        })
      );
      if (live) renderHealth({ ts: ev.ts, checks: p.checks });
      add(el);
      break;
    }
    case "draft": {
      const card = p.card.trim().toUpperCase();
      let draft = state.drafts.get(card);
      if (!draft) {
        const el = $("#tpl-draft").content.firstElementChild.cloneNode(true);
        el.dataset.key = `e${ev.id}`;
        el.dataset.kind = "note";
        fillIcons(el);
        $(".svc-icon", el).innerHTML = ICONS.draft;
        $(".draft-card", el).textContent = card;
        draft = { el, key: el.dataset.key, card, versions: [], text: "" };
        state.drafts.set(card, draft);
        $(".copy-draft", el).addEventListener("click", async (e) => {
          const button = e.currentTarget;
          await navigator.clipboard.writeText(draft.text);
          flash(button, "Copiado");
        });
        $(".draft-history", el).addEventListener("click", () => openDraftHistory(draft));
        add(el);
      }
      draft.versions.push(ev);
      draft.text = p.text;
      $("time", draft.el).textContent = timeFmt.format(new Date(ev.ts));
      $(".note-text", draft.el).textContent = p.text;
      const history = $(".draft-history", draft.el);
      history.hidden = draft.versions.length < 2;
      history.textContent = `Editado · ${draft.versions.length} versiones`;
      history.setAttribute("aria-label", `Ver historial de ${card}: ${draft.versions.length} versiones`);
      if (draftHistory.open && draftHistory.dataset.card === card) renderDraftHistory(draft);
      state.draft = draft;
      summary = true;
      break;
    }
    case "query.started": {
      const el = queryCard(ev, p);
      $(".query-text", el).textContent = p.query || "";
      $(".params pre", el).textContent = JSON.stringify(p.params, null, 2);
      setStatus(el, "running", "corriendo");
      wireResult(el);
      if (p.cancellable) {
        const btn = $(".cancel", el);
        btn.hidden = false;
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          const r = await action(`/api/queries/${ev.query_id}/cancel`);
          if (!r.ok) btn.disabled = false;
        });
      }
      const svc = svcOf(p.tool);
      const sources = sourcesOf(p);
      addSources(sources);
      state.cards.set(ev.query_id, { el, data: null, svc, sources });
      bump(svc, { total: 1, running: 1 });
      summary = true;
      add(el);
      break;
    }
    case "query.progress": {
      const card = state.cards.get(ev.query_id);
      if (!card) break;
      addProgress(card, ev.ts, p.message);
      break;
    }
    case "query.approval_requested": {
      const card = state.cards.get(ev.query_id);
      if (!card) break;
      setStatus(card.el, "waiting", "esperando aprobación");
      card.waiting = true;
      state.waiting += 1;
      showApproval(card, ev);
      break;
    }
    case "query.approved":
    case "query.denied": {
      const card = state.cards.get(ev.query_id);
      if (!card) break;
      hideApproval(card);
      const approved = ev.type === "query.approved";
      setStatus(card.el, "running", approved ? "corriendo" : "rechazando…");
      addProgress(card, ev.ts, approved ? "aprobada en el visor" : "rechazada en el visor");
      break;
    }
    case "query.rejected": {
      const el = queryCard(ev, p);
      $(".query-text", el).textContent = p.params?.sql || p.params?.query || "";
      $(".params pre", el).textContent = JSON.stringify(p.params, null, 2);
      setStatus(el, "rejected", "rechazada");
      $(".meta", el).textContent = "bloqueada antes de ejecutarse";
      el.dataset.error = "1";
      const err = $(".error", el);
      err.hidden = false;
      err.textContent = p.reason;
      bump(svcOf(p.tool), { total: 1, rejected: 1 });
      const sources = sourcesOf({ ...p, query: p.params?.sql });
      addSources(sources);
      markSourceErrors(sources);
      summary = true;
      add(el);
      break;
    }
    case "query.cancel_requested": {
      const card = state.cards.get(ev.query_id);
      if (!card) break;
      setStatus(card.el, "running", "cancelando…");
      $(".cancel", card.el).disabled = true;
      break;
    }
    case "query.finished": {
      const card = state.cards.get(ev.query_id);
      if (!card) break;
      hideApproval(card);
      $(".cancel", card.el).hidden = true;
      bump(card.svc, { running: -1, cost: Number(p.meta?.cost_usd || 0) });
      setStatus(card.el, "ok", "ok");
      $(".meta", card.el).textContent = metaText(p);
      renderKnowledgeLinks(card.el, p.knowledge);
      card.data = p.result;
      renderResult(card);
      break;
    }
    case "query.failed": {
      const card = state.cards.get(ev.query_id);
      if (!card) break;
      hideApproval(card);
      $(".cancel", card.el).hidden = true;
      bump(card.svc, { running: -1, error: 1 });
      setStatus(card.el, "failed", "error");
      $(".meta", card.el).textContent = `${p.duration_ms} ms`;
      card.el.dataset.error = "1";
      const err = $(".error", card.el);
      err.hidden = false;
      err.textContent = p.error;
      renderKnowledgeLinks(card.el, p.knowledge);
      markSourceErrors(card.sources);
      summary = true;
      break;
    }
  }

  const emailCard = state.cards.get(ev.query_id);
  if (emailCard) refreshEmailVisibility(emailCard.el);

  if (live) {
    renderHead();
    if (summary) renderSummary();
    applyFilters();
  }
}

function addProgress(card, ts, message) {
  const li = document.createElement("li");
  li.textContent = `${timeFmt.format(new Date(ts))} · ${message}`;
  $(".progress", card.el).append(li);
}

function showApproval(card, ev) {
  const panel = $(".approval", card.el);
  panel.hidden = false;
  $(".approval-reason", panel).textContent = ev.payload.reason;
  const buttons = [$(".approve", panel), $(".deny", panel)];
  buttons.forEach((btn) => {
    btn.disabled = false;
    btn.onclick = async () => {
      buttons.forEach((b) => (b.disabled = true));
      const r = await action(`/api/queries/${ev.query_id}/approval`, { approve: btn.classList.contains("approve") });
      if (!r.ok) buttons.forEach((b) => (b.disabled = false));
    };
  });
  const deadline = new Date(ev.ts).getTime() + ev.payload.timeout_s * 1000;
  const tick = () => {
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    $(".countdown", panel).textContent = left ? `se rechaza sola en ${left} s` : "plazo vencido";
    if (!left) clearInterval(card.timer);
  };
  clearInterval(card.timer);
  card.timer = setInterval(tick, 1000);
  tick();
}

function hideApproval(card) {
  clearInterval(card.timer);
  $(".approval", card.el).hidden = true;
  if (card.waiting) {
    card.waiting = false;
    state.waiting -= 1;
  }
}

function queryCard(ev, p) {
  const el = $("#tpl-query").content.firstElementChild.cloneNode(true);
  fillIcons(el);
  const svc = svcOf(p.tool);
  el.dataset.key = ev.query_id;
  el.dataset.kind = svc || "other";
  if (svc) {
    el.classList.add(`svc-${svc}`);
    $(".svc-icon", el).innerHTML = ICONS[svc];
    $(".svc-icon", el).title = SERVICES[svc];
  }
  $(".tool", el).textContent = p.tool;
  const env = p.params?.env;
  if (env && env !== "default") {  // «default»: una fuente sin `environment`, no dice nada
    const pill = $(".env-pill", el);
    pill.hidden = false;
    pill.textContent = env.toUpperCase();
    pill.dataset.tone = envTone(env);
    pill.title = `Entorno ${env}`;
  }
  $(".purpose", el).textContent = p.purpose || "consulta";
  $("time", el).textContent = timeFmt.format(new Date(ev.ts));
  const text = p.query || p.params?.sql || p.params?.query;
  if (text) {
    const btn = $(".copy-query", el);
    btn.hidden = false;
    btn.addEventListener("click", async (e) => {
      const button = e.currentTarget;
      await navigator.clipboard.writeText(emailDisplay(el, text));
      flash(button, "Copiada");
    });
  }
  return el;
}

// Lo más nuevo va arriba. Si el ingeniero está leyendo más abajo, se compensa el scroll para que no salte.
function prepend(el, live) {
  const timeline = $("#timeline");
  el.hidden = !cardMatches(el);
  const reading = window.scrollY > 120;
  const before = document.documentElement.scrollHeight;
  timeline.prepend(el);
  if (reading) window.scrollBy(0, document.documentElement.scrollHeight - before);
  if (live) el.classList.add("fresh");
}

function bump(svc, delta) {
  if (!svc) return;
  const st = state.stats[svc];
  for (const [k, v] of Object.entries(delta)) st[k] += v;
}

function setStatus(el, cls, label) {
  const s = $(".status", el);
  s.className = `status ${cls}`;
  s.textContent = label;
}

// El color de la etiqueta del entorno (va después del ícono del servicio): prod (y prod-archive, production…) en rojo, para que se note; los nombres
// conocidos con su color y cualquier otro, siempre el mismo de una paleta fija.
const ENV_TONES = ["note", "ok"];  // sin azul ni naranja (MySQL y CloudWatch) ni rosa (se confunde con prod)
function envTone(env) {
  const name = env.toLowerCase();
  if (/^(prod|prd|production|live)\b/.test(name)) return "prod";
  if (/^(qa|test|testing|uat)\b/.test(name)) return "qa";
  if (/^(staging|stage|stg|preprod|pre)\b/.test(name)) return "staging";
  if (/^(dev|develop|development|local|lab|sandbox)\b/.test(name)) return "dev";
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return ENV_TONES[h % ENV_TONES.length];
}

function metaText(p) {
  const parts = [`${p.rows} filas`, `${p.duration_ms} ms`];
  if (p.meta?.cost_usd != null) parts.push(`$${Number(p.meta.cost_usd).toFixed(4)}`);
  else if (p.meta?.estimated_cost_usd != null) parts.push(`estimado $${Number(p.meta.estimated_cost_usd).toFixed(4)}`);
  if (p.meta?.total_hits != null) parts.push(`${p.meta.total_hits} hits totales`);
  if (p.meta?.truncated) parts.push("truncado");
  return parts.join(" · ");
}

function esc(text) {
  const d = document.createElement("div");
  d.textContent = text;
  return d.innerHTML;
}

// --- tabla de resultados ---------------------------------------------------------------------------------

// La ocultación es visual: los originales se conservan en el historial y para el agente.
const emailText = new WeakMap();
function maskEmails(text) {
  return text.replace(/\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b/g, "$1***@$2");
}
function emailDisplay(el, text) {
  return el.dataset.emailsVisible === "true" ? text : maskEmails(text);
}
function refreshEmailVisibility(el) {
  const button = $(".toggle-emails", el);
  if (!button) return;
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let node, hasEmails = false;
  while ((node = walker.nextNode())) {
    if (node.parentElement.closest("button")) continue;
    const previous = emailText.get(node);
    const raw = previous && node.nodeValue === previous.shown ? previous.raw : node.nodeValue;
    const shown = emailDisplay(el, raw);
    if (maskEmails(raw) !== raw) hasEmails = true;
    emailText.set(node, { raw, shown });
    if (node.nodeValue !== shown) node.nodeValue = shown;
  }
  button.hidden = !hasEmails;
  const visible = el.dataset.emailsVisible === "true";
  button.textContent = visible ? "Ocultar correos" : "Mostrar correos";
  button.setAttribute("aria-pressed", String(visible));
}

function wireResult(el) {
  $(".toggle-emails", el).addEventListener("click", () => {
    el.dataset.emailsVisible = String(el.dataset.emailsVisible !== "true");
    refreshEmailVisibility(el);
  });
  el.querySelectorAll(".tabs button").forEach((btn) =>
    btn.addEventListener("click", () => {
      el.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b === btn)));
      const json = btn.dataset.tab === "json";
      $(".tables", el).hidden = json;
      $(".json", el).hidden = !json;
    })
  );
}

function renderResult(card) {
  const { el, data } = card;
  $(".result", el).hidden = false;
  const tables = [{ name: data.tables?.length ? "resultado" : "", columns: data.columns, rows: data.rows }, ...(data.tables || [])];
  const raw = data.raw ?? data.rows.map((r) => Object.fromEntries(data.columns.map((c, i) => [c, r[i]])));
  $(".json", el).textContent = JSON.stringify(raw, null, 2);
  $(".tables", el).replaceChildren(...tables.filter((t) => t.columns.length).map((table) => tableBlock(table, el)));
}

function tableBlock({ name, columns, rows }, queryEl) {
  const block = $("#tpl-table").content.firstElementChild.cloneNode(true);
  $(".table-name", block).textContent = name;
  $(".table-count", block).textContent = name ? `${rows.length} filas` : "";

  const numeric = columns.map((_, i) => rows.length > 0 && rows.every((r) => r[i] == null || isNumeric(r[i])));
  const view = { sortCol: null, dir: 1, filter: "" };

  const thead = $("thead", block);
  const tr = document.createElement("tr");
  columns.forEach((c, i) => {
    const th = document.createElement("th");
    th.textContent = c;
    th.addEventListener("click", () => {
      view.dir = view.sortCol === i ? -view.dir : 1;
      view.sortCol = i;
      thead.querySelectorAll("th").forEach((h) => h.removeAttribute("data-sort"));
      th.dataset.sort = view.dir === 1 ? "asc" : "desc";
      draw();
    });
    tr.append(th);
  });
  thead.replaceChildren(tr);

  $(".filter", block).addEventListener("input", (e) => {
    view.filter = e.target.value.toLowerCase();
    draw();
  });
  $(".copy-csv", block).addEventListener("click", async (e) => {
    const button = e.currentTarget;
    await navigator.clipboard.writeText(emailDisplay(queryEl, toCsv(columns, visibleRows())));
    flash(button, "Copiado");
  });

  function visibleRows() {
    let out = rows;
    if (view.filter) out = out.filter((r) => r.some((v) => cell(v).toLowerCase().includes(view.filter)));
    if (view.sortCol != null) {
      const i = view.sortCol;
      out = [...out].sort((a, b) => compare(a[i], b[i]) * view.dir);
    }
    return out;
  }

  function draw() {
    $("tbody", block).replaceChildren(
      ...visibleRows().map((r) => {
        const row = document.createElement("tr");
        r.forEach((v, i) => {
          const td = document.createElement("td");
          td.textContent = cell(v);
          if (numeric[i]) td.className = "num";
          row.append(td);
        });
        return row;
      })
    );
    refreshEmailVisibility(queryEl);
  }
  draw();
  return block;
}

// Cambia el texto del botón un momento (respeta el ícono si lo tiene).
function flash(btn, text) {
  const label = btn.querySelector("span:last-child") || btn;
  const original = label.textContent;
  label.textContent = text;
  setTimeout(() => (label.textContent = original), 1200);
}

function cell(v) {
  if (v == null) return "";
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

// CloudWatch devuelve los números como texto ("51877").
function isNumeric(v) {
  return typeof v === "number" || (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v));
}

function compare(a, b) {
  if (a == null) return b == null ? 0 : 1;
  if (b == null) return -1;
  if (isNumeric(a) && isNumeric(b)) return Number(a) - Number(b);
  return cell(a).localeCompare(cell(b), undefined, { numeric: true });
}

function toCsv(columns, rows) {
  const q = (v) => {
    const s = cell(v);
    return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
  };
  return [columns.map(q).join(","), ...rows.map((r) => r.map(q).join(","))].join("\n");
}

// --- conexiones ------------------------------------------------------------------------------------------

// Los sistemas (y su nombre corto) llegan en /api/meta, desde config.toml.
const connName = (check) => (state.meta.systems || []).find((s) => s.check === check)?.short || check;
const RANK = { ok: 0, warn: 1, fail: 2 };
let health = null;

function renderHealth(h) {
  health = h;
  const list = $("#conn-list");
  // Un sistema puede tener varios chequeos (túnel y sesión): se muestra el peor.
  const worst = new Map();
  for (const c of h?.checks || []) {
    const prev = worst.get(c.name);
    if (!prev) worst.set(c.name, { ...c, details: [c.detail] });
    else {
      prev.details.push(c.detail);
      if (RANK[c.status] > RANK[prev.status]) Object.assign(prev, { status: c.status, detail: c.detail });
    }
  }
  // Los chequeos históricos pueden ser anteriores a la incorporación de un servicio.
  for (const { check: name } of state.meta.systems || []) {
    if (!worst.has(name)) worst.set(name, {
      name, status: "unknown", details: ["Sin chequear. Actualiza el estado de conexiones."],
    });
  }
  const words = { ok: "ok", warn: "aviso", fail: "falla", unknown: "sin chequear" };
  list.replaceChildren(
    ...[...worst.values()].map((c) => {
      const li = document.createElement("li");
      li.className = c.status;
      li.title = c.details.join("\n");
      li.innerHTML = `<span class="dot"></span><span class="name"></span><span class="state"></span>`;
      $(".name", li).textContent = connName(c.name);
      $(".state", li).textContent = words[c.status] || c.status;
      return li;
    })
  );
  $("#conn-when").textContent = h ? relTime(h.ts) : "";
  $("#conn-when").title = h ? dateFmt.format(new Date(h.ts)) : "";
  if (typeof settingsView !== "undefined") settingsView.onHealth(h);
}

// Corre doctor en el servidor (sin reparar túneles). Lo usan el panel lateral y Ajustes.
let rechecking = null;
function recheckHealth() {
  if (rechecking) return rechecking;
  const btn = $("#recheck");
  btn.disabled = true;
  btn.classList.add("spinning");
  $("#conn-when").textContent = "revisando…";
  rechecking = (async () => {
    try {
      const r = await action("/api/health");
      renderHealth(r.ok ? await r.json() : health);
    } catch {
      renderHealth(health);
    } finally {
      btn.disabled = false;
      btn.classList.remove("spinning");
      rechecking = null;
    }
  })();
  return rechecking;
}
$("#recheck").innerHTML = ICONS.refresh;
$("#recheck").addEventListener("click", recheckHealth);

// --- en vivo ---------------------------------------------------------------------------------------------

function connect(after) {
  const src = new EventSource(`/api/stream?after=${after}`);
  src.onopen = () => $("#conn").classList.add("live");
  src.onerror = () => $("#conn").classList.remove("live");
  src.onmessage = async (msg) => {
    // En la demo de la guía no se pinta nada real; al salir se recarga todo.
    if (typeof onboarding !== "undefined" && onboarding.demo) return;
    const ev = JSON.parse(msg.data);
    if (ev.type === "session.started" || ev.type === "session.resumed") {
      await loadSessions();
      if ($("#follow").checked || state.current == null) {
        // En Ajustes no se cambia de pantalla: la sesión nueva queda lista para cuando vuelva.
        if (currentView() === "sessions") location.hash = `#/sessions/${ev.session_id}`;
        else await openSession(ev.session_id);
        return;
      }
    } else {
      const s = state.sessions.find((x) => x.id === ev.session_id);
      if (s) {
        if (ev.type === "session.updated") {
          s.name = ev.payload.name;
          s.card = ev.payload.card;
        }
        s.events += 1;
        s.last_event_at = ev.ts;
        s.last_event_id = ev.id;
        if (ev.type === "query.failed" || ev.type === "query.rejected") s.errors += 1;
        if (ev.type === "slack") s.slack = 1;
      }
      if (ev.type === "health" && ev.session_id !== state.current) renderHealth({ ts: ev.ts, checks: ev.payload.checks });
    }
    applyEvent(ev, true);
    renderSessions();
  };
}

// --- pantallas: #/sessions/<id>[/<tarjeta>] y #/settings ------------------------------------------------

function currentView() {
  if (location.hash.startsWith("#/settings")) return "settings";
  if (location.hash.startsWith("#/knowledge")) return "knowledge";
  if (location.hash.startsWith("#/skills")) return "skills";
  return "sessions";
}

function showView(view) {
  $("#sessions-view").hidden = view !== "sessions";
  $("#sessions-side").hidden = view !== "sessions";
  $("#settings-view").hidden = view !== "settings";
  $("#skills-view").hidden = view !== "skills";
  $("#knowledge-view").hidden = view !== "knowledge";
  document.querySelectorAll(".nav-item").forEach((a) =>
    a.dataset.view === view ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")
  );
  const titles = { knowledge: "Conocimiento · ", settings: "Ajustes · ", skills: "Skills · " };
  document.title = `${titles[view] || ""}Dev Support Viewer`;
}

function route() {
  const view = currentView();
  showView(view);
  if (view === "settings") {
    settingsView.show();
    return;
  }
  if (view === "knowledge") {
    knowledgeView.show();
    return;
  }
  if (view === "skills") {
    skillsView.show();
    return;
  }
  const id = sessionFromHash();
  if (id == null) {
    // "#/sessions" a secas (el link de la barra): vuelve a la sesión que estaba abierta.
    if (state.current != null) history.replaceState(null, "", `#/sessions/${state.current}`);
    return;
  }
  if (id !== state.current) openSession(id);
  else focusCard(cardKeyFromHash());
}

window.addEventListener("hashchange", route);

// Los "hace X min" envejecen solos.
setInterval(() => {
  renderSessions();
  renderHead();
  if (health && !rechecking) $("#conn-when").textContent = relTime(health.ts);
}, 30_000);

// --- búsqueda entre sesiones -----------------------------------------------------------------------------

const KIND = {
  note: "nota",
  slack: "slack",
  draft: "borrador",
  "query.started": "consulta",
  "query.finished": "resultado",
  "query.failed": "error",
  "query.rejected": "rechazada",
};

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(e.target.value.trim()), 250);
});

async function runSearch(q) {
  const results = $("#search-results");
  const searching = q.length >= 2;
  $("#sessions").hidden = searching;
  $(".session-filters").hidden = searching;
  if (searching) $("#session-pager").hidden = true;
  else renderSessions();
  results.hidden = !searching;
  if (!searching) return;
  const hits = await fetch(`/api/search?q=${encodeURIComponent(q)}`).then((r) => r.json());
  if ($("#search").value.trim() !== q) return; // llegó tarde: ya se escribió otra cosa
  if (!hits.length) {
    results.innerHTML = `<p class="empty">Sin resultados.</p>`;
    return;
  }
  results.replaceChildren(
    ...hits.map((h) => {
      const a = document.createElement("a");
      a.href = `#/sessions/${h.session_id}/${h.query_id || "e" + h.event_id}`;
      a.innerHTML = `<span class="name"></span><span class="snippet"></span>`;
      $(".name", a).textContent = `${h.session} `;
      const kind = document.createElement("span");
      kind.className = "kind";
      kind.textContent = `· ${KIND[h.type] || h.type} · ${dateFmt.format(new Date(h.ts))}`;
      $(".name", a).append(kind);
      highlight($(".snippet", a), h.snippet, q);
      return a;
    })
  );
}

function highlight(el, text, q) {
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) {
    el.textContent = text;
    return;
  }
  const mark = document.createElement("mark");
  mark.textContent = text.slice(i, i + q.length);
  el.replaceChildren(text.slice(0, i), mark, text.slice(i + q.length));
}

(async function main() {
  fillIcons();
  // Se fija el cursor antes de cargar, así no se pierde nada entre la carga y la conexión SSE.
  // meta y health pueden faltar si el servidor es de una versión anterior (el estático se lee del disco).
  const optional = (url, fallback) => fetch(url).then((r) => (r.ok ? r.json() : fallback), () => fallback);
  const [{ latest_event_id }, meta, lastHealth] = await Promise.all([
    fetch("/api/state").then((r) => r.json()),
    optional("/api/meta", state.meta),
    optional("/api/health", null),
  ]);
  state.meta = meta;
  // Textos del pack: el filtro de cards y el ejemplo de la ayuda.
  $('.seg-btn[data-origin="card"] .label').textContent = meta.card_prefix || "Cards";
  document.querySelectorAll(".card-example").forEach((el) => (el.textContent = meta.card_example || "CASO-1234"));
  $("#version").textContent = meta.version ? `v${meta.version}` : "";
  renderHealth(lastHealth);
  void recheckHealth();
  await loadSessions();
  const id = sessionFromHash() ?? state.sessions[0]?.id ?? null;
  if (id != null && currentView() === "sessions" && sessionFromHash() == null) {
    history.replaceState(null, "", `#/sessions/${id}`);
  }
  showView(currentView());
  await openSession(id);
  if (currentView() === "settings") settingsView.show();
  skillsView.init(); // el aviso de la barra lateral si una skill del repo no está bien instalada
  if (currentView() === "skills") skillsView.show();
  if (currentView() === "knowledge") knowledgeView.show();
  connect(latest_event_id);
})();
