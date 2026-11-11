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
  const pageSize = 20;
  let pageOffset = 0;
  let appliedFilters = new URLSearchParams(new FormData(filters));
  const labels = {verified: "Verificado", pending: "Por verificar", stale: "Desactualizado"};
  const tones = {verified: "ok", pending: "warn", stale: "fail"};
  const sourceIcons = {mysql: "db", opensearch: "os", cloudwatch: "cw", code: "terminal"};
  const sourceNames = {mysql: "MySQL", opensearch: "OpenSearch", cloudwatch: "CloudWatch", code: "Código"};
  function stateTag(status) { return el("span", labels[status] || status, `state-tag ${tones[status] || "none"}`); }
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
        ? `${data.total} entrada${data.total > 1 ? "s" : ""}`
        : "Sin entradas";
      const pages = Math.max(1, Math.ceil(data.total / pageSize));
      pageLabel.textContent = `${Math.floor(offset / pageSize) + 1} / ${pages}`;
      pageLabel.closest("nav").hidden = pages < 2;
      for (const item of data.entries) {
        // Mismo diseño que la lista de Skills: ícono de la fuente, título, estado; resumen; fuente y fecha.
        const a = el("a", "", "skill-item knowledge-item");
        a.href = `#/knowledge/${item.id}`;
        if (item.id === selectedId) a.setAttribute("aria-current", "true");
        const top = el("span", "", "skill-top");
        const icon = el("span", "", "btn-icon");
        icon.setAttribute("aria-hidden", "true");
        icon.innerHTML = ICONS[sourceIcons[item.source]] || ICONS.db;
        top.append(icon, el("span", item.title, "skill-name"), stateTag(item.status));
        const desc = ["schema", "catalog", "mapping"].includes(item.kind) ? kinds[item.kind] : item.summary;
        a.append(top, el("span", desc || "Sin resumen.", "skill-desc"),
          el("span", `${sourceNames[item.source] || item.source} · ${item.environment} · ${item.verified_at ? `verificado ${relTime(item.verified_at)}` : "sin verificar"}`, "skill-meta"));
        a.querySelector(".skill-name").dataset.tip = item.title;
        list.append(a);
      }
      if (!data.total) list.append(el("p", "No hay coincidencias. Puedes importar los esquemas guardados en tus sesiones o capturarlos en próximas consultas.", "empty"));
      // La entrada abierta debe verse en la lista lateral, como en Skills.
      // Solo se mueve la lista lateral y solo si la entrada quedó fuera de vista (no tapa los filtros).
      const current = list.querySelector("[aria-current]");
      const side = list.closest(".knowledge-side");
      if (current && side && side.scrollHeight > side.clientHeight) {
        const c = current.getBoundingClientRect(), box = side.getBoundingClientRect();
        if (c.top < box.top) side.scrollTop -= box.top - c.top + 8;
        else if (c.bottom > box.bottom) side.scrollTop += c.bottom - box.bottom + 8;
      }
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
      const header = el("header", "", "skill-header");
      const title = el("div", "", "skill-title");
      title.append(el("h2", item.title), stateTag(item.status));
      header.append(title, el("p", `${kinds[item.kind] || item.kind} · revisión ${item.revision}`, "skill-state-text"));
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
      const actions = el("div", "", "skill-actions");
      actions.append(edit, remove);
      header.append(actions);
      const meta = el("dl", "", "skill-meta-list");
      for (const [label, value, mono] of [
        ["Fuente", sourceNames[item.source] || item.source], ["Entorno", item.environment, true],
        ["Entidad", item.entity, true], ["Temas", item.topics.join(", ") || "Sin etiquetas"],
        ["Última verificación", date(item.verified_at)]]) {
        meta.append(el("dt", label), el("dd", value, mono ? "mono" : ""));
      }
      const content = el("section", "", "knowledge-section");
      content.append(el("h3", "Contenido"), el("pre", readable(item.content), "knowledge-content"));
      detail.replaceChildren(header, meta,
        el("p", "Verificado confirma la observación del esquema en esa fecha, no su vigencia actual ni hechos de un cliente.", "knowledge-note"),
        content, evidenceView(item.evidence));
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
  // Los filtros se aplican mientras se escribe (como en Skills); Enter los aplica al instante.
  let filterTimer = null;
  function applyFilters() {
    clearTimeout(filterTimer);
    const nextFilters = new URLSearchParams(new FormData(filters));
    if (nextFilters.toString() === appliedFilters.toString()) return;
    appliedFilters = nextFilters;
    load(0).catch(fail);
  }
  filters.onsubmit = e => { e.preventDefault(); applyFilters(); };
  filters.addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(applyFilters, 250); });
  filters.addEventListener("change", applyFilters);
  previous.onclick = () => load(Math.max(0, pageOffset - pageSize)).catch(fail);
  next.onclick = () => { if (nextOffset !== null) load(nextOffset).catch(fail); };
  document.querySelector("#knowledge-reload").onclick = () => {
    if (editing) { message.textContent = "Guarda o cancela la corrección antes de actualizar."; return; }
    void show();
  };
  const noticeBox = document.querySelector("#knowledge-notice");
  const sourceLabel = r => `${sourceNames[r.source] || r.source} · ${r.environment}`;
  // Snackbar del resultado: queda abajo hasta cerrarlo; el detalle completo se abre en el modal.
  function notice(title, text, tone = "info", result = null) {
    const close = el("button", "", "icon-btn");
    close.type = "button"; close.setAttribute("aria-label", "Cerrar aviso"); close.dataset.icon = "close";
    close.onclick = () => { noticeBox.hidden = true; };
    const body = el("div", "", "text");
    body.append(el("strong", title), el("p", text));
    const parts = [body];
    if (result && result.items && result.items.length) {
      const detail = el("button", "Ver detalle", "ghost detail");
      detail.type = "button";
      detail.onclick = () => openImportModal(title, result, {done: true});
      parts.push(detail);
    }
    noticeBox.className = `knowledge-notice ${tone}`;
    noticeBox.replaceChildren(...parts, close);
    fillIcons(noticeBox);
    noticeBox.hidden = false;
    noticeBox.style.animation = "none"; void noticeBox.offsetWidth; noticeBox.style.animation = "";
    noticeBox.focus({preventScroll: true});
  }
  function counts(r) {
    const parts = [`${r.new} nuevas`];
    if (r.updated) parts.push(`${r.updated} actualizadas`);
    parts.push(`${r.existing} ya las tenías`);
    if (r.kept) parts.push(`${r.kept} más viejas que las tuyas`);
    if (r.invalid) parts.push(`${r.invalid} inválidas`);
    return parts.join(" · ") + ".";
  }
  function resultNotice(what, r) {
    const changed = r.new + r.updated;
    const tone = r.invalid ? "warn" : changed ? "ok" : "info";
    const title = changed ? `${what}: ${changed} ${changed === 1 ? "entrada importada" : "entradas importadas"}` : `${what}: no había nada nuevo`;
    notice(title, counts(r), tone, r);
  }
  // Un solo modal para la vista previa (confirmar) y para el detalle de lo importado (con enlaces).
  // Resuelve true si la persona confirma.
  const modal = document.querySelector("#knowledge-import-modal");
  function openImportModal(title, data, {done = false} = {}) {
    const changed = data.new + data.updated;
    modal.querySelector("#knowledge-import-title").textContent = title;
    modal.querySelector(".modal-desc").textContent = done
      ? "Esto es lo que entró. Abre una entrada para revisarla."
      : changed ? "Revisa qué va a entrar antes de importar. Las entradas que ya tienes no se tocan."
        : "No hay nada nuevo para importar: ya tienes todo lo de este origen.";
    const chips = [["new", "nuevas", "ok"], ["updated", "actualizadas", "warn"], ["existing", "ya las tenías", "none"],
      ["kept", "más viejas que las tuyas", "none"], ["invalid", "inválidas", "fail"]]
      .filter(([key]) => data[key] || key === "new" || key === "existing")
      .map(([key, label, tone]) => el("span", `${data[key]} ${label}`, `state-tag ${tone}`));
    modal.querySelector(".knowledge-import-counts").replaceChildren(...chips);
    const rows = (data.items || []).map(item => {
      const li = el("li", "");
      let name = el("span", item.title, "title");
      if (done && item.id) {
        const a = el("a", item.title, "title");
        a.href = `#/knowledge/${item.id}`;
        a.onclick = () => modal.close();
        name = a;
      }
      li.append(el("span", item.change === "updated" ? "Actualiza" : "Nueva", `state-tag ${item.change === "updated" ? "warn" : "ok"}`),
        name, el("small", sourceLabel(item)));
      return li;
    });
    if (changed > rows.length) rows.push(el("li", `y ${changed - rows.length} más`));
    modal.querySelector(".knowledge-import-items").replaceChildren(...rows);
    const notes = [];
    if (data.updated) notes.push(done ? "Las versiones anteriores de las actualizadas quedan en su historial."
      : "Las actualizadas reemplazan tu versión por una más reciente; la tuya queda en el historial.");
    if (data.invalid) notes.push("Las inválidas se omitieron.");
    modal.querySelector(".knowledge-import-note").textContent = notes.join(" ");
    const confirm = modal.querySelector(".modal-confirm");
    const cancel = modal.querySelector(".modal-cancel");
    confirm.hidden = done || !changed;
    confirm.textContent = `Importar ${changed}`;
    cancel.textContent = confirm.hidden ? "Cerrar" : "Cancelar";
    return new Promise(resolve => {
      let answer = false;
      const finish = value => { answer = value; if (modal.open) modal.close(); };
      confirm.onclick = () => finish(true);
      cancel.onclick = modal.querySelector(".modal-close").onclick = () => finish(false);
      modal.onclick = e => { if (e.target === modal) finish(false); };
      modal.addEventListener("close", () => resolve(answer), {once: true});
      modal.showModal();
      (confirm.hidden ? cancel : confirm).focus();
    });
  }
  async function importWithPreview(button, what, busy, run) {
    if (editing) { notice("Hay una corrección sin guardar", "Guárdala o cancélala antes de importar.", "warn"); return; }
    button.disabled = true;
    message.textContent = busy;
    try {
      const preview = await run(true);
      message.textContent = "";
      if (!await openImportModal(what, preview)) return;
      message.textContent = busy;
      const result = await run(false);
      message.textContent = "";
      await load();
      resultNotice(what, result);
    } catch (error) {
      message.textContent = "";
      notice(`${what}: no se pudo`, error.message, "fail");
    } finally { button.disabled = false; }
  }
  document.querySelector("#knowledge-import").onclick = async e => {
    const button = e.currentTarget; button.disabled = true;
    message.textContent = "Importando metadatos locales; no se consultan servicios…";
    try {
      const result = await request("/api/knowledge/import", {method: "POST", headers: {"x-casebook-viewer": "1"}});
      message.textContent = "";
      await load();
      notice(result.changed ? `Desde sesiones: ${result.changed} actualizaciones` : "Desde sesiones: no había nada nuevo",
        "Repetir la importación no duplica entradas." + (result.warnings ? ` ${result.warnings} observaciones no pudieron importarse.` : ""),
        result.warnings ? "warn" : result.changed ? "ok" : "info");
    } catch(error) { message.textContent = ""; notice("Desde sesiones: no se pudo", error.message, "fail"); }
    finally { button.disabled = false; }
  };
  document.querySelector("#knowledge-export").onclick = () => { location.href = "/api/knowledge/export"; };
  const fileInput = document.querySelector("#knowledge-file");
  const importFile = document.querySelector("#knowledge-import-file");
  importFile.onclick = () => fileInput.click();
  fileInput.onchange = async () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    const body = await file.text();
    await importWithPreview(importFile, `Importar ${file.name}`, `Leyendo ${file.name}…`, dryRun =>
      request(`/api/knowledge/import-file?name=${encodeURIComponent(file.name)}&dry_run=${dryRun}`,
        {method: "POST", headers: {"content-type": "application/json", "x-casebook-viewer": "1"}, body}));
  };
  const pull = document.querySelector("#knowledge-pull");
  request("/api/knowledge/share").then(info => { pull.hidden = !info.configured; }).catch(() => {});
  pull.onclick = () => importWithPreview(pull, "Traer del equipo", "Consultando la rama del equipo…", dryRun =>
    request(`/api/knowledge/pull?dry_run=${dryRun}`, {method: "POST", headers: {"x-casebook-viewer": "1"}}));
  window.addEventListener("beforeunload", e => { if (editing) { e.preventDefault(); e.returnValue = ""; } });
  return {show};
})();
