// Datos de conocimiento siempre como texto; nunca ejecutamos consultas ni Markdown de las entradas.
function renderKnowledgeLinks(card, knowledge) {
  if (!knowledge) return;
  const box = document.createElement("p");
  box.className = "knowledge-links";
  if (knowledge.warning) box.textContent = knowledge.warning;
  else {
    box.append("Conocimiento actualizado: ");
    for (const item of knowledge.entries || []) {
      const a = document.createElement("a");
      a.href = `#/knowledge/${item.id}`;
      a.textContent = `#${item.id} `;
      box.append(a);
    }
  }
  card.append(box);
}

const knowledgeView = (() => {
  const filters = document.querySelector("#knowledge-filters");
  const list = document.querySelector("#knowledge-list");
  const detail = document.querySelector("#knowledge-detail");
  const message = document.querySelector("#knowledge-message");
  const previous = document.querySelector("#knowledge-prev");
  const next = document.querySelector("#knowledge-next");
  const pageLabel = document.querySelector("#knowledge-page");
  const pageSize = 5;
  let pageOffset = 0;
  let appliedFilters = new URLSearchParams(new FormData(filters));
  const labels = {verified: "Verificado", pending: "Por verificar", stale: "Desactualizado"};
  const kinds = {schema: "Columnas e índices", catalog: "Tablas observadas", mapping: "Campos del índice",
    relation: "Relación", query: "Consulta de ejemplo", reference: "Referencia", limitation: "Limitación"};
  function readable(content) {
    try { return JSON.stringify(JSON.parse(content), null, 2); } catch { return content; }
  }
  let nextOffset = null;
  let generation = 0;
  let listingGeneration = 0;
  let editing = false;
  let selectedId = null;

  async function request(url, options) {
    const r = await fetch(url, options);
    const body = await r.json();
    if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Error ${r.status}`);
    return body;
  }
  function fail(error) { message.textContent = error.message; }
  function date(value) { return value ? new Date(value).toLocaleString() : "No verificado"; }
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function button(label, icon, cls = "") {
    const b = el("button", "", cls);
    b.type = "button";
    if (ICONS[icon]) {
      const glyph = el("span", "", "btn-icon");
      glyph.setAttribute("aria-hidden", "true");
      glyph.innerHTML = ICONS[icon];
      b.append(glyph);
    }
    b.append(el("span", label));
    return b;
  }
  async function load(offset = pageOffset) {
    const ticket = ++listingGeneration;
    previous.disabled = next.disabled = true;
    list.setAttribute("aria-busy", "true");
    try {
      const params = new URLSearchParams(appliedFilters);
      params.set("offset", offset);
      params.set("limit", pageSize);
      const data = await request(`/api/knowledge?${params}`);
      if (ticket !== listingGeneration) return;
      // Tras eliminar la última entrada de una página, vuelve a la última página disponible.
      if (offset > 0 && offset >= data.total) {
        return await load(Math.max(0, Math.ceil(data.total / pageSize) - 1) * pageSize);
      }
      pageOffset = offset;
      nextOffset = data.next_offset;
      list.replaceChildren();
      document.querySelector("#knowledge-count").textContent = data.total
        ? `${offset + 1}–${offset + data.entries.length} de ${data.total} entradas`
        : "0 entradas";
      pageLabel.textContent = `Página ${Math.floor(offset / pageSize) + 1} de ${Math.max(1, Math.ceil(data.total / pageSize))}`;
      for (const item of data.entries) {
        const a = el("a", "", "knowledge-item");
        a.href = `#/knowledge/${item.id}`;
        if (item.id === selectedId) a.setAttribute("aria-current", "true");
        a.append(el("strong", item.title), el("span", `${item.source} · ${item.environment}`),
          el("span", labels[item.status], `knowledge-status ${item.status}`),
          el("span", ["schema", "catalog", "mapping"].includes(item.kind) ? kinds[item.kind] : item.summary, "knowledge-snippet"), el("small", `Verificación: ${date(item.verified_at)}`));
        list.append(a);
      }
      if (!data.total) list.append(el("p", "No hay coincidencias. Puedes importar los esquemas guardados en tus sesiones o capturarlos en próximas consultas.", "empty"));
    } finally {
      if (ticket === listingGeneration) {
        previous.disabled = pageOffset === 0;
        next.disabled = nextOffset === null;
        list.removeAttribute("aria-busy");
      }
    }
  }
  function evidenceView(evidence) {
    const box = el("section", "", "knowledge-evidence");
    box.append(el("h3", "Evidencia"), el("p", evidence.summary || ""));
    if (evidence.session_id && evidence.query_id) {
      const a = el("a", `Sesión ${evidence.session_id} · consulta ${evidence.query_id}`);
      a.href = `#/sessions/${encodeURIComponent(evidence.session_id)}/${encodeURIComponent(evidence.query_id)}`;
      box.append(a);
      box.append(el("small", "La sesión original puede haberse eliminado; el resumen se conserva aquí."));
    }
    if (evidence.code_path) box.append(el("pre", `${evidence.code_path}\nRevisión: ${evidence.code_revision}`));
    return box;
  }
  function historyItem(item) {
    const row = document.createElement("details");
    row.append(el("summary", `Revisión ${item.revision} · ${labels[item.status]} · ${date(item.updated_at)}`),
      el("p", item.reason));
    let loaded = false;
    row.addEventListener("toggle", async () => {
      if (!row.open || loaded) return;
      loaded = true;
      try {
        const snapshot = await request(`/api/knowledge/${item.id}?revision=${item.revision}`);
        row.append(el("pre", readable(snapshot.content)), evidenceView(snapshot.evidence));
      } catch(e) { loaded = false; fail(e); }
    });
    return row;
  }
  function editForm(item) {
    editing = true;
    const form = document.createElement("form");
    form.className = "knowledge-edit";
    const header = el("header", "", "knowledge-edit-head");
    const heading = el("div", "");
    heading.append(el("span", "CONOCIMIENTO / EDICIÓN", "knowledge-eyebrow"),
      el("h2", "Editar conocimiento"), el("p", "Actualiza lo aprendido y deja el motivo para la próxima investigación."));
    header.append(heading, el("span", `Revisión ${item.revision}`, "knowledge-revision"));
    form.append(header);
    const grid = el("div", "", "knowledge-edit-fields");
    for (const [name, label, value, multiline] of [
      ["title", "Título", item.title, false], ["topics", "Temas", item.topics.join(", "), false],
      ["content", "Contenido", readable(item.content), true], ["reason", "Motivo de la corrección", "", false]]) {
      const field = el("label", "", `knowledge-field knowledge-field-${name}`);
      const caption = el("span", label, "knowledge-label");
      const input = document.createElement(multiline ? "textarea" : "input");
      input.name = name; input.value = value; input.required = name !== "topics";
      if (multiline) { input.rows = 14; input.spellcheck = false; }
      if (name === "topics") input.placeholder = "ventas, integraciones, atribución";
      if (name === "reason") input.placeholder = "¿Qué cambió y qué evidencia lo respalda?";
      field.append(caption, input);
      if (name === "topics") field.append(el("small", "Separa los temas con comas."));
      if (name === "content") field.append(el("small", "Esquemas, relaciones o consultas reutilizables. Sin datos de clientes."));
      grid.append(field);
    }
    form.append(grid);
    const note = el("div", "", "knowledge-edit-note");
    note.append(el("span", "Por verificar", "knowledge-status pending"),
      el("span", "La corrección conserva las versiones anteriores y queda pendiente de verificación."));
    form.append(note);
    const save = button("Guardar corrección", "check", "knowledge-primary"); save.type = "submit";
    const stale = button("Marcar desactualizada", "warn", "knowledge-stale");
    const cancel = button("Cancelar", null, "ghost");
    const footer = el("footer", "", "knowledge-edit-footer");
    const mainActions = el("div", "", "knowledge-main-actions");
    mainActions.append(cancel, save);
    footer.append(stale, mainActions);
    const error = el("p", "", "knowledge-edit-error"); error.setAttribute("role", "alert");
    form.append(error, footer);
    async function submit(markStale) {
      if (!form.reportValidity()) return;
      const values = Object.fromEntries(new FormData(form));
      values.topics = values.topics.split(",").map(x => x.trim()).filter(Boolean);
      values.expected_revision = item.revision; values.stale = markStale;
      save.disabled = stale.disabled = cancel.disabled = true;
      form.setAttribute("aria-busy", "true");
      error.textContent = "";
      try {
        await request(`/api/knowledge/${item.id}`, {method: "PUT", headers: {"content-type": "application/json", "x-casebook-viewer": "1"}, body: JSON.stringify(values)});
        editing = false;
        await show();
        message.textContent = "Cambio guardado con historial.";
      } catch (e) { error.textContent = e.message; }
      finally { save.disabled = stale.disabled = cancel.disabled = false; form.removeAttribute("aria-busy"); }
    }
    form.onsubmit = e => { e.preventDefault(); void submit(false); };
    stale.onclick = () => submit(true);
    cancel.onclick = () => { editing = false; void show(); };
    detail.replaceChildren(form);
  }
  async function show() {
    const id = Number(location.hash.split("/")[2]) || null;
    if (editing && id === selectedId) return;
    editing = false;
    selectedId = id;
    const ticket = ++generation;
    message.textContent = "";
    try {
      await load();
      if (ticket !== generation) return;
      if (!id) { detail.replaceChildren(el("p", "Selecciona una entrada para ver su contenido y evidencia.", "empty")); return; }
      const item = await request(`/api/knowledge/${id}`);
      if (ticket !== generation) return;
      const heading = el("h2", item.title);
      const edit = button("Corregir o marcar desactualizada", "note"); edit.onclick = () => editForm(item);
      const remove = button("Eliminar entrada", "trash", "danger");
      remove.onclick = async () => {
        if (!window.confirm(`¿Eliminar «${item.title}» y todo su historial? Esta acción no se puede deshacer. Las sesiones originales se conservan.`)) return;
        remove.disabled = true;
        try {
          await request(`/api/knowledge/${item.id}`, {method: "DELETE",
            headers: {"content-type": "application/json", "x-casebook-viewer": "1"},
            body: JSON.stringify({expected_revision: item.revision})});
          window.history.replaceState(null, "", "#/knowledge");
          await show();
          message.textContent = "Entrada eliminada.";
        } catch(e) { fail(e); remove.disabled = false; }
      };
      const actions = el("div", "", "knowledge-actions");
      actions.append(edit, remove);
      detail.replaceChildren(heading,
        el("p", `${item.source} · ${item.environment} · ${kinds[item.kind] || item.kind} · ${labels[item.status]}`),
        el("p", `Entidad: ${item.entity}`), el("p", `Temas: ${item.topics.join(", ") || "Sin etiquetas"}`),
        el("p", `Última verificación: ${date(item.verified_at)} · Revisión ${item.revision}`),
        el("p", "Verificado confirma la observación del esquema en esa fecha, no su vigencia actual ni hechos de un cliente.", "knowledge-note"),
        actions, el("pre", readable(item.content), "knowledge-content"), evidenceView(item.evidence));
      const history = el("section", "", "knowledge-history");
      history.append(el("h3", "Historial de cambios"));
      item.history.forEach(h => history.append(historyItem(h)));
      const older = el("button", "Ver revisiones anteriores");
      let offset = item.next_history_offset;
      older.hidden = offset === null;
      older.onclick = async () => {
        older.disabled = true;
        try {
          const data = await request(`/api/knowledge/${id}?history_offset=${offset}`);
          data.history.forEach(h => history.insertBefore(historyItem(h), older));
          offset = data.next_history_offset; older.hidden = offset === null;
        } catch(e) { fail(e); } finally { older.disabled = false; }
      };
      history.append(older); detail.append(history);
    } catch(e) { fail(e); if (!editing) detail.replaceChildren(el("p", e.message, "empty")); }
  }
  filters.onsubmit = e => {
    e.preventDefault();
    appliedFilters = new URLSearchParams(new FormData(filters));
    load(0).catch(fail);
  };
  previous.onclick = () => load(Math.max(0, pageOffset - pageSize)).catch(fail);
  next.onclick = () => { if (nextOffset !== null) load(nextOffset).catch(fail); };
  document.querySelector("#knowledge-reload").onclick = () => {
    if (editing) { message.textContent = "Guarda o cancela la corrección antes de actualizar."; return; }
    void show();
  };
  document.querySelector("#knowledge-import").onclick = async e => {
    const button = e.currentTarget; button.disabled = true;
    message.textContent = "Importando metadatos locales; no se consultan servicios…";
    try {
      const result = await request("/api/knowledge/import", {method: "POST", headers: {"x-casebook-viewer": "1"}});
      await load();
      message.textContent = `${result.changed} actualizaciones de conocimiento. ${result.warnings} observaciones no pudieron importarse. Repetir la importación no duplica entradas.`;
    } catch(error) { fail(error); } finally { button.disabled = false; }
  };
  window.addEventListener("beforeunload", e => { if (editing) { e.preventDefault(); e.returnValue = ""; } });
  return {show};
})();
