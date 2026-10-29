// Ajustes: formulario sobre config.toml (GET/PUT /api/settings), estado de credenciales y tema del navegador.
// Usa lo que define app.js: $, ICONS, action, esc, relTime, recheckHealth, health, applyTheme, storedTheme.

const FIELDS = {
  "viewer.host": { label: "Host del visor", help: "Dónde escucha el visor. Solo direcciones locales." },
  "viewer.port": { label: "Puerto del visor", help: "Si está ocupado, el servidor asume que ya hay otro visor abierto." },
  "viewer.auto_open": { label: "Abrir el visor solo", help: "En la primera <code>session_start</code>, si no hay ninguna pestaña conectada." },
  "jira.base_url": { label: "Link a las cards", help: "Base de los links de “Abrir card”, por ejemplo <code>https://&lt;sitio&gt;.atlassian.net/browse/</code>. Vacío lo oculta.", placeholder: "https://…/browse/" },
  "viewer.approval_timeout_s": { label: "Tiempo para aprobar", help: "Pasado este plazo, una consulta que pidió aprobación se rechaza sola.", unit: "s", step: 1 },
  "cloudwatch.max_cost_usd": { label: "Tope de CloudWatch por consulta", help: "Si el estimado (×1.5 de margen) lo supera, la consulta pide aprobación. Claude puede pasar otro tope en cada consulta.", unit: "USD", step: 0.01 },
  "cloudwatch.max_window_days": { label: "Ventana máxima de CloudWatch", help: "Días que puede abarcar una consulta o un estimado.", unit: "días" },
  "opensearch.max_size": { label: "Tamaño máximo de OpenSearch", help: "<code>size</code> (y <code>from + size</code>) máximo de <code>os_search</code>.", unit: "docs" },
  "db.default_limit": { label: "LIMIT por defecto de MySQL", help: "Se agrega a un SELECT que no trae LIMIT.", unit: "filas" },
  "db.max_limit": { label: "LIMIT máximo de MySQL", help: "Un SELECT con un LIMIT mayor se rechaza.", unit: "filas" },
  "db.full_scan_max_rows": { label: "Full scan permitido", help: "Filas estimadas por el EXPLAIN para un full scan antes de pedir aprobación.", unit: "filas" },
  "db.plan_max_rows": { label: "Plan máximo", help: "Producto de filas estimadas del plan antes de pedir aprobación.", unit: "filas" },
  "slack.watch": { label: "Vigilar el canal", help: "Escucha las notificaciones de Slack de escritorio (no gasta tokens) y analiza cada pedido nuevo con la skill de triage. Lee el canal con el conector de Slack de claude.ai; nunca escribe en Slack." },
  "slack.channel_id": { label: "Canal", help: "ID del canal de soporte (por defecto, el del pack).", placeholder: "C…" },
  "slack.channel_name": { label: "Nombre del canal", help: "Cómo aparece en las notificaciones de Slack, sin <code>#</code>. En Slack, el canal tiene que notificar «Todos los mensajes nuevos»." },
  "slack.backup_poll_minutes": { label: "Revisión de respaldo", help: "Lee el canal cada tantos minutos aunque no llegue una notificación (Slack cerrado, «No molestar» o el canal abierto en pantalla). Cada revisión gasta uso de tu suscripción. 0 la apaga.", unit: "min" },
  "slack.default_lookback_minutes": { label: "Ventana de inicio por defecto", help: "Minutos de mensajes anteriores a revisar al encender la vigilancia. 0 = solo mensajes en tiempo real.", unit: "min" },
  "slack.ask_lookback_on_enable": { label: "Preguntar al activar vigilancia", help: "Muestra un diálogo para elegir la ventana de tiempo (tiempo real, 1h, 3h, etc.) cada vez que se enciende el switch de Slack." },
  "slack.poll_model": { label: "Modelo para leer el canal", help: "Solo trae los mensajes nuevos cuando llega una notificación: conviene el más barato." },
  "slack.analysis_model": { label: "Modelo para analizar", help: "El que hace el triage de cada pedido." },
  "slack.max_analyses_per_day": { label: "Análisis por día", help: "Pasado el tope, los pedidos quedan registrados sin analizar.", unit: "análisis" },
  "slack.code_paths": { label: "Código local", help: "Carpetas con el código, separadas por coma. El análisis las lee <strong>solo en lectura</strong> (sin ejecutar ni modificar nada, y sin credenciales como <code>.env</code>) para entender cómo funciona algo o confirmar un posible bug. Vacío = no lee código.", placeholder: "/home/yo/repos/backend" },
  "db.max_execution_ms": { label: "Tiempo máximo de MySQL", help: "<code>MAX_EXECUTION_TIME</code> de cada consulta: la base la corta al pasarlo.", unit: "ms" },
};

const SECTIONS = [
  { id: "visor", title: "Visor", desc: "Cómo se sirve y se comporta esta página.",
    keys: ["viewer.host", "viewer.port", "viewer.auto_open", "jira.base_url"] },
  { id: "guardas", title: "Guardas y costos", desc: "Límites que se revisan antes de correr una consulta. Lo que los supera pide aprobación aquí o se rechaza.",
    keys: ["cloudwatch.max_cost_usd", "viewer.approval_timeout_s", "cloudwatch.max_window_days", "opensearch.max_size",
      "db.default_limit", "db.max_limit", "db.full_scan_max_rows", "db.plan_max_rows", "db.max_execution_ms"] },
  { id: "slack", title: "Slack", desc: "Vigilancia del canal de soporte: solo lectura, con el conector de claude.ai.",
    keys: ["slack.watch", "slack.channel_id", "slack.channel_name", "slack.backup_poll_minutes", "slack.default_lookback_minutes",
      "slack.ask_lookback_on_enable", "slack.poll_model", "slack.analysis_model", "slack.max_analyses_per_day", "slack.code_paths"] },
  { id: "fuentes", title: "Fuentes", desc: "De dónde consulta Claude: las del pack del equipo y las que agregues aquí (van a tu config.toml). Una fuente nueva aparece en Claude al reiniciarlo." },
  { id: "credenciales", title: "Credenciales", desc: "El login sigue siendo en tu terminal (el prompt es oculto, no funciona con !). Aquí ves el estado y copias el comando." },
  { id: "conexiones", title: "Conexiones", desc: "Hosts y túneles. Se editan a mano en config.toml." },
  { id: "apariencia", title: "Apariencia", desc: "Solo afecta a este navegador." },
  { id: "guia", title: "Primeros pasos", desc: "Un recorrido por el visor con un caso de ejemplo." },
];

const settingsView = (() => {
  let data = null; // la respuesta de /api/settings
  let draft = {}; // clave -> valor editado (solo lo que difiere del archivo)
  let rendered = false;
  const root = $("#settings-view");
  const fmtNum = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

  const byKey = (key) => data.settings.find((s) => s.key === key);
  const same = (a, b) => (a ?? null) === (b ?? null);

  async function load() {
    const r = await fetch("/api/settings");
    data = await r.json();
    // Credenciales lista los sistemas de /api/meta; si se entró directo a Ajustes puede no haber llegado todavía.
    if (!state.meta.systems?.length) {
      const m = await fetch("/api/meta").then((res) => (res.ok ? res.json() : null), () => null);
      if (m) state.meta = m;
    }
    draft = {};
    render();
  }

  // Al entrar a Ajustes desde otra vista, sin cambios a medio editar, se vuelve a leer: el archivo pudo cambiar desde
  // otra parte (otra pestaña o a mano; el toggle de Slack avisa con onExternalChange). Los links del índice no
  // recargan.
  let inView = false;
  window.addEventListener("hashchange", () => {
    if (!location.hash.startsWith("#/settings")) inView = false;
  });

  async function show() {
    const entering = !inView;
    inView = true;
    if (!rendered || (entering && !Object.keys(draft).length)) await load();
    const anchor = location.hash.split("/")[2];
    if (anchor) document.getElementById(`set-${anchor}`)?.scrollIntoView({ block: "start" });
    else window.scrollTo(0, 0);
  }

  function render() {
    rendered = true;
    const err = $(".settings-error", root);
    err.hidden = !data.error;
    err.textContent = data.error || "";
    $(".settings-links", root).replaceChildren(
      ...SECTIONS.map((sec) => {
        const a = document.createElement("a");
        a.href = `#/settings/${sec.id}`;
        a.textContent = sec.title;
        return a;
      })
    );
    $(".settings-path", root).innerHTML = data.path
      ? `Se guarda en <code>${esc(data.path)}</code>${data.exists ? "" : " (todavía no existe)"}. Al guardar queda una copia en <code>config.toml.bak</code>. Las credenciales siguen en el keyring; el visor no las lee.`
      : "Este visor no tiene un config.toml asociado: los ajustes son solo de lectura.";
    $(".settings-sections", root).replaceChildren(...SECTIONS.map(section));
    renderSavebar();
  }

  function section(sec) {
    const el = document.createElement("section");
    el.className = "section";
    el.id = `set-${sec.id}`;
    el.innerHTML = `<h3></h3><p class="desc"></p>`;
    $("h3", el).textContent = sec.title;
    $(".desc", el).textContent = sec.desc;
    if (sec.keys) sec.keys.forEach((k) => el.append(field(byKey(k))));
    if (sec.id === "fuentes") sourcesSection(el);
    if (sec.id === "credenciales") credentials(el);
    if (sec.id === "conexiones") connections(el);
    if (sec.id === "apariencia") appearance(el);
    if (sec.id === "guia") guide(el);
    return el;
  }

  // --- campos -------------------------------------------------------------------------------------------

  function current(s) {
    return s.key in draft ? draft[s.key] : s.value;
  }

  function field(s) {
    const meta = FIELDS[s.key] || { label: s.key, help: "" };
    const row = document.createElement("div");
    row.className = "field";
    row.dataset.key = s.key;
    row.innerHTML = `<div class="field-label"><div class="field-name"><span></span></div><p class="field-help"></p></div>
      <div class="field-control"></div>`;
    $(".field-name span", row).textContent = meta.label;
    $(".field-control", row).append(control(s, meta));
    paintField(row, s);
    return row;
  }

  // Etiquetas y ayuda que dependen del valor (modificado, reinicio pendiente, de fábrica).
  function paintField(row, s) {
    const meta = FIELDS[s.key] || { help: "" };
    const name = $(".field-name", row);
    name.querySelectorAll(".tag").forEach((t) => t.remove());
    const value = current(s);
    const tag = (cls, text, title) => {
      const t = document.createElement("span");
      t.className = `tag ${cls}`;
      t.textContent = text;
      if (title) t.title = title;
      name.append(t);
    };
    if (s.key in draft) tag("changed", "modificado");
    if (s.restart && !same(s.value, s.running)) tag("restart", "pendiente de reiniciar", `Ahora corre con ${s.running}`);
    else if (s.restart) tag("restart", "requiere reiniciar Claude Code");

    const help = $(".field-help", row);
    help.innerHTML = meta.help;
    if (!same(value, s.default)) {
      const shown = s.default === null ? "vacío" : typeof s.default === "boolean" ? (s.default ? "sí" : "no") : fmtValue(s.default);
      help.insertAdjacentHTML("beforeend", ` De fábrica: ${esc(shown)}. `);
      const reset = document.createElement("button");
      reset.className = "reset";
      reset.textContent = "Restablecer";
      reset.addEventListener("click", () => setValue(s, s.default, true));
      help.append(reset);
    }
  }

  function fmtValue(v) {
    return typeof v === "number" ? fmtNum.format(v) : String(v);
  }

  function setValue(s, value, rerender = false) {
    if (same(value, s.value)) delete draft[s.key];
    else draft[s.key] = value;
    const row = root.querySelector(`.field[data-key="${CSS.escape(s.key)}"]`);
    if (rerender) {
      const ctl = $(".field-control", row);
      ctl.replaceChildren(control(s, FIELDS[s.key] || {}));
    }
    paintField(row, s);
    renderSavebar();
  }

  function control(s, meta) {
    const readOnly = !data.path;
    const value = current(s);
    if (s.type === "bool") {
      const b = document.createElement("button");
      b.className = "switch";
      b.setAttribute("role", "switch");
      b.setAttribute("aria-checked", String(!!value));
      b.setAttribute("aria-label", meta.label);
      b.disabled = readOnly;
      b.addEventListener("click", () => {
        const next = b.getAttribute("aria-checked") !== "true";
        b.setAttribute("aria-checked", String(next));
        setValue(s, next);
      });
      return b;
    }
    if (s.choices) {
      const sel = document.createElement("select");
      sel.className = "select-input";
      sel.disabled = readOnly;
      sel.setAttribute("aria-label", meta.label);
      s.choices.forEach((c) => sel.add(new Option(c, c, false, c === value)));
      sel.addEventListener("change", () => setValue(s, sel.value));
      return sel;
    }
    if (s.type === "int" || s.type === "float") {
      const wrap = document.createElement("label");
      wrap.className = "num-input";
      wrap.innerHTML = `<input type="number" inputmode="decimal"><span class="unit"></span>`;
      const input = $("input", wrap);
      input.value = value;
      input.min = s.min ?? "";
      input.max = s.max ?? "";
      input.step = meta.step ?? (s.type === "int" ? 1 : "any");
      input.disabled = readOnly;
      input.setAttribute("aria-label", meta.label);
      $(".unit", wrap).textContent = meta.unit || "";
      wrap.title = `Entre ${fmtValue(s.min)} y ${fmtValue(s.max)}`;
      input.addEventListener("input", () => {
        const n = input.value === "" ? NaN : Number(input.value);
        const ok = Number.isFinite(n) && n >= s.min && n <= s.max && (s.type !== "int" || Number.isInteger(n));
        wrap.classList.toggle("invalid", !ok);
        setValue(s, ok ? n : input.value);
      });
      return wrap;
    }
    const input = document.createElement("input");
    input.className = "text-input";
    input.value = value ?? "";
    input.placeholder = meta.placeholder || "";
    input.disabled = readOnly;
    input.setAttribute("aria-label", meta.label);
    input.addEventListener("input", () => {
      const v = input.value.trim();
      setValue(s, v === "" && s.optional ? null : v);
    });
    return input;
  }

  // --- barra de guardado --------------------------------------------------------------------------------

  function renderSavebar() {
    const bar = $(".savebar", root);
    const keys = Object.keys(draft);
    bar.hidden = !keys.length;
    $(".save-error", bar).hidden = true;
    $(".save-count", bar).textContent = keys.length === 1 ? "1 cambio sin guardar" : `${keys.length} cambios sin guardar`;
    const restart = keys.some((k) => byKey(k).restart);
    $(".save-note", bar).textContent = restart
      ? "Host y puerto se aplican al reiniciar Claude Code; lo demás, en la próxima consulta."
      : "Se escriben en config.toml y el servidor los toma en la próxima consulta.";
    $(".save-list", bar).replaceChildren(
      ...keys.map((k) => {
        const li = document.createElement("li");
        const s = byKey(k);
        li.innerHTML = `<code></code>: <span class="from"></span> → <strong class="to"></strong>`;
        $("code", li).textContent = k;
        $(".from", li).textContent = s.value ?? "vacío";
        $(".to", li).textContent = draft[k] ?? "vacío";
        return li;
      })
    );
  }

  $(".save-diff", root).addEventListener("click", (e) => {
    const list = $(".save-list", root);
    list.hidden = !list.hidden;
    e.currentTarget.setAttribute("aria-expanded", String(!list.hidden));
    e.currentTarget.textContent = list.hidden ? "Ver cambios" : "Ocultar cambios";
  });

  $(".save-discard", root).addEventListener("click", () => {
    draft = {};
    render();
  });

  $(".save-apply", root).addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { "x-casebook-viewer": "1", "content-type": "application/json" },
        body: JSON.stringify({ values: draft }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        const err = $(".save-error", root);
        err.hidden = false;
        err.textContent = typeof body.detail === "string" ? body.detail : "No se pudo guardar.";
        return;
      }
      data = body;
      draft = {};
      render();
      flashSaved();
      if (typeof slackPanel !== "undefined") slackPanel.refresh(); // el toggle de la barra lateral
      // "Abrir card" depende de jira.base_url.
      state.meta.jira_base_url = byKey("jira.base_url").running;
    } finally {
      btn.disabled = false;
    }
  });

  function flashSaved() {
    const note = document.createElement("p");
    note.className = "settings-saved";
    note.textContent = "Guardado.";
    note.style.cssText = "margin:0;color:var(--ok);font-weight:600";
    $(".settings-col", root).prepend(note);
    setTimeout(() => note.remove(), 2500);
  }

  // --- credenciales y conexiones ------------------------------------------------------------------------

  function credentials(el) {
    (state.meta.systems || []).forEach((c) => {
      const row = document.createElement("div");
      row.className = "cred";
      row.dataset.check = c.check;
      row.innerHTML = `<span class="cred-name"><span class="label"></span><span class="cred-detail"></span></span>
        <span class="state-tag none">sin chequear</span>
        <span class="cmd"><code></code><button class="icon-btn copy-cmd" title="Copiar el comando" aria-label="Copiar el comando">${ICONS.copy}</button></span>`;
      $(".label", row).textContent = c.label;
      $(".cmd code", row).textContent = c.cmd;
      $(".copy-cmd", row).addEventListener("click", async (e) => {
        await navigator.clipboard.writeText(c.cmd);
        const b = e.currentTarget;
        b.innerHTML = svg('<path d="m5 12.5 5 4.5 9-10"/>');
        setTimeout(() => (b.innerHTML = ICONS.copy), 1200);
      });
      el.append(row);
    });
    const foot = document.createElement("div");
    foot.className = "section-foot";
    foot.innerHTML = `<button class="recheck-all"><span class="btn-icon">${ICONS.refresh}</span><span>Revisar ahora</span></button>
      <span class="when"></span>`;
    $(".recheck-all", foot).addEventListener("click", async (e) => {
      const b = e.currentTarget;
      b.disabled = true;
      $(".when", foot).textContent = "revisando…";
      await recheckHealth();
      b.disabled = false;
    });
    el.append(foot);
    paintCredentials(el);
  }

  function paintCredentials(el = document.getElementById("set-credenciales")) {
    if (!el) return;
    const h = health;
    const words = { ok: "vigente", warn: "con aviso", fail: "falla" };
    const rank = { ok: 0, warn: 1, fail: 2 };
    el.querySelectorAll(".cred").forEach((row) => {
      const checks = (h?.checks || []).filter((c) => c.name === row.dataset.check);
      const tagEl = $(".state-tag", row);
      const detail = $(".cred-detail", row);
      if (!checks.length) {
        tagEl.className = "state-tag none";
        tagEl.textContent = "sin chequear";
        detail.textContent = "Actualiza el estado de conexiones para revisar esta credencial.";
        return;
      }
      const worst = checks.reduce((a, b) => (rank[b.status] > rank[a.status] ? b : a));
      tagEl.className = `state-tag ${worst.status}`;
      tagEl.textContent = words[worst.status] || worst.status;
      detail.textContent = worst.detail;
      detail.title = checks.map((c) => c.detail).join("\n");
    });
    const when = $(".section-foot .when", el);
    if (when) when.textContent = h ? `Último chequeo ${relTime(h.ts)}. Equivale a casebook doctor, sin reparar túneles.` : "Todavía no hay chequeos.";
  }

  // --- fuentes -------------------------------------------------------------------------------------------
  // La lista sale de /api/sources y el formulario de «Agregar» se arma con los campos que declara cada proveedor.

  const AUTH = { browser: "login en Chrome", secret: "usuario/clave en el keyring", external: "externa", none: "sin sesión", missing: "falta el proveedor" };

  async function sourcesSection(el) {
    el.innerHTML = `<p class="sources-pack muted"></p><div class="table-wrap ro-table"><table><thead><tr><th>Id</th><th>Fuente</th><th>Proveedor</th><th>Sesión</th><th>Origen</th><th></th></tr></thead><tbody></tbody></table></div>
      <p class="sources-msg" hidden></p>
      <details class="sources-add"><summary>Agregar una fuente</summary><form class="sources-form"></form></details>`;
    let payload;
    try {
      payload = await fetch("/api/sources").then((r) => r.json());
    } catch {
      el.textContent = "No pude leer las fuentes.";
      return;
    }
    const msg = $(".sources-msg", el);
    const say = (text, bad = false) => {
      msg.hidden = !text;
      msg.textContent = text || "";
      msg.style.color = bad ? "var(--fail)" : "var(--ok)";
    };
    $(".sources-pack", el).textContent = payload.pack
      ? `Pack activo: ${payload.pack.name} ${payload.pack.version}. Se cambia con casebook pack install.`
      : "Sin pack activo: instala el de tu equipo con casebook pack install, o agrega fuentes aquí.";
    const tbody = $("tbody", el);
    if (!payload.sources.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted">No hay fuentes configuradas.</td></tr>`;
    }
    payload.sources.forEach((s) => {
      const tr = document.createElement("tr");
      [s.id, s.label, s.provider, AUTH[s.auth] || s.auth, s.origin].forEach((v) => {
        const td = document.createElement("td");
        td.textContent = v;
        tr.append(td);
      });
      const td = document.createElement("td");
      const test = document.createElement("button");
      test.className = "ghost";
      test.textContent = "Probar";
      test.addEventListener("click", async () => {
        test.disabled = true;
        say(`Probando ${s.id}…`);
        const r = await action(`/api/sources/${encodeURIComponent(s.id)}/test`);
        const data = r.ok ? await r.json() : { checks: [{ status: "fail", detail: await r.text() }] };
        const bad = data.checks.some((c) => c.status === "fail");
        say(`${s.id}: ` + data.checks.map((c) => c.detail).join(" · "), bad);
        test.disabled = false;
      });
      td.append(test);
      if (payload.writable && s.origin.includes("config")) {
        const del = document.createElement("button");
        del.className = "ghost";
        del.textContent = s.origin === "config" ? "Quitar" : "Volver al pack";
        del.addEventListener("click", async () => {
          const r = await fetch(`/api/sources/${encodeURIComponent(s.id)}`, { method: "DELETE", headers: { "x-casebook-viewer": "1" } });
          if (r.ok) {
            say(`${s.id}: ${(await r.json()).result}.`);
            sourcesSection(el);
          } else say((await r.json()).detail || "No se pudo quitar.", true);
        });
        td.append(" ", del);
      }
      tr.append(td);
      tbody.append(tr);
    });
    if (!payload.writable) {
      $(".sources-add", el).hidden = true;
      return;
    }
    sourceForm($(".sources-form", el), payload.catalog, async (body) => {
      const r = await action("/api/sources", body);
      const data = await r.json();
      if (!r.ok) return say(data.detail || "No se pudo guardar.", true);
      await sourcesSection(el);
      const note = $(".sources-msg", el);
      note.hidden = false;
      note.style.color = "var(--ok)";
      note.textContent = `Guardada como ${data.added}. Inicia sesión si hace falta (Credenciales) y reinicia Claude Code para verla.`;
    });
  }

  function sourceForm(form, catalog, submit) {
    const kinds = Object.keys(catalog);
    form.innerHTML = `<label>Tipo <select name="kind">${kinds.map((k) => `<option value="${k}">${esc(catalog[k].label)}</option>`).join("")}</select></label>
      <label>Proveedor <select name="provider"></select></label>
      <label class="env-row">Entorno <input name="env" placeholder="staging" autocomplete="off"></label>
      <div class="provider-fields"></div><div class="actions"><button type="submit" class="primary">Guardar</button></div>`;
    const kindSel = $('[name="kind"]', form);
    const provSel = $('[name="provider"]', form);
    const fields = $(".provider-fields", form);
    const fieldRow = (f) => {
      const label = document.createElement("label");
      label.textContent = f.label + (f.required ? " *" : "") + " ";
      let input;
      if (f.choices) {
        input = document.createElement("select");
        input.innerHTML = f.choices.map((c) => `<option ${c === f.default ? "selected" : ""}>${esc(c)}</option>`).join("");
      } else if (f.type === "bool") {
        input = document.createElement("select");
        input.innerHTML = `<option value="true" ${f.default !== false ? "selected" : ""}>sí</option><option value="false" ${f.default === false ? "selected" : ""}>no</option>`;
      } else {
        input = document.createElement("input");
        input.autocomplete = "off";
        input.placeholder = f.default != null && f.default !== "" ? String(f.default) : f.type === "list" ? "a, b, c" : "";
      }
      input.name = `f:${f.key}`;
      label.append(input);
      if (f.help) {
        const help = document.createElement("small");
        help.className = "muted";
        help.textContent = " " + f.help;
        label.append(help);
      }
      return label;
    };
    const renderProviders = () => {
      const kind = catalog[kindSel.value];
      provSel.innerHTML = kind.providers.map((p) => `<option value="${p.name}">${esc(p.label)} (${p.name})</option>`).join("");
      $(".env-row", form).hidden = kindSel.value !== "opensearch";
      renderFields();
    };
    const renderFields = () => {
      const kind = catalog[kindSel.value];
      const provider = kind.providers.find((p) => p.name === provSel.value);
      fields.replaceChildren(...(provider ? provider.fields : []).map(fieldRow), ...kind.meta.map(fieldRow));
    };
    kindSel.addEventListener("change", renderProviders);
    provSel.addEventListener("change", renderFields);
    renderProviders();
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const values = {};
      form.querySelectorAll('[name^="f:"]').forEach((input) => {
        if (input.value.trim() !== "") values[input.name.slice(2)] = input.value.trim();
      });
      submit({ kind: kindSel.value, provider: provSel.value, env: $('[name="env"]', form).value.trim() || null, values });
    });
  }

  function connections(el) {
    const ro = data.readonly;
    const table = document.createElement("div");
    table.className = "table-wrap ro-table";
    table.innerHTML = `<table><thead><tr><th>OpenSearch</th><th>Proveedor</th><th>Dominio o URL</th><th>Puerto</th><th>Host ssh</th></tr></thead><tbody></tbody></table>`;
    $("tbody", table).replaceChildren(
      ...ro.opensearch.map((e) => {
        const tr = document.createElement("tr");
        [e.env, e.provider || "dashboards-proxy", e.domain, e.tunnel_port, e.ssh_host].forEach((v, i) => {
          const td = document.createElement("td");
          td.textContent = v;
          if (i === 3) td.className = "num";
          tr.append(td);
        });
        return tr;
      })
    );
    const dl = document.createElement("dl");
    dl.className = "ro-list";
    const rows = [
      ["MySQL (reader)", `${ro.db.host} / ${ro.db.database}`],
      ["Túnel de MySQL", ro.db.ssh_host],
      ["CloudWatch", ro.cloudwatch],
      ["Base de sesiones", ro.db_path],
    ];
    rows.forEach(([k, v]) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      dl.append(dt, dd);
    });
    el.append(table, dl);
  }

  // --- apariencia ---------------------------------------------------------------------------------------

  function appearance(el) {
    const row = document.createElement("div");
    row.className = "field";
    row.innerHTML = `<div class="field-label"><div class="field-name">Tema</div><p class="field-help">“Sistema” sigue al modo claro u oscuro del sistema operativo.</p></div>
      <div class="field-control"><div class="segmented" role="group" aria-label="Tema"></div></div>`;
    const seg = $(".segmented", row);
    const options = [["system", "Sistema"], ["light", "Claro"], ["dark", "Oscuro"]];
    const paint = () => seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.theme === storedTheme())));
    options.forEach(([value, label]) => {
      const b = document.createElement("button");
      b.dataset.theme = value;
      b.textContent = label;
      b.addEventListener("click", () => {
        try {
          if (value === "system") localStorage.removeItem(THEME_KEY);
          else localStorage.setItem(THEME_KEY, value);
        } catch {
          // sin almacenamiento: el tema vale solo hasta recargar
        }
        applyTheme(value);
        paint();
      });
      seg.append(b);
    });
    paint();
    el.append(row);
  }

  // --- primeros pasos ------------------------------------------------------------------------------------

  function guide(el) {
    const row = document.createElement("div");
    row.className = "field";
    row.innerHTML = `<div class="field-label"><div class="field-name">Guía de primeros pasos</div><p class="field-help">Llega un caso, sus
      consultas corren en vivo, una pide aprobación por costo y termina en un borrador; después, Slack y estos ajustes. Tus sesiones
      se ocultan mientras dura y vuelven igual al terminar: nada se consulta ni se guarda.</p></div>
      <div class="field-control"><button class="primary">Empezar guía</button></div>`;
    $("button", row).addEventListener("click", () => onboarding.start());
    el.append(row);
  }

  // Otro control guardó estas claves (por ejemplo el toggle de Slack de la barra lateral): se toma la respuesta del
  // servidor y se descarta lo que estuviera a medio editar en esas claves, que ya quedó viejo.
  function onExternalChange(body, keys) {
    if (!rendered) return;
    data = body;
    keys.forEach((k) => delete draft[k]);
    render();
  }

  return {
    show,
    onHealth: () => rendered && paintCredentials(),
    onExternalChange,
  };
})();
