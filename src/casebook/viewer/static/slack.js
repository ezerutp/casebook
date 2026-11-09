// Panel de Slack en la barra lateral: toggle de la vigilancia (slack.watch en config.toml), estado del vigilante y
// el análisis manual del último mensaje. El análisis de cada pedido queda como una sesión normal.

const slackPanel = (() => {
  const panel = $("#slack-panel");
  const toggle = $("#slack-toggle");
  let st = null;

  async function refresh() {
    try {
      const r = await fetch("/api/slack");
      if (!r.ok) return; // servidor de una versión anterior: el panel queda oculto
      st = await r.json();
    } catch {
      return;
    }
    render();
  }

  function render() {
    panel.hidden = false;
    toggle.setAttribute("aria-checked", String(st.enabled));
    // Lo que antes decía el panel con la vigilancia apagada ahora lo dice el interruptor al pasar el mouse.
    toggle.dataset.tip = `Vigilancia ${st.enabled ? "activa" : "apagada"}. Nunca escribe en Slack.`;
    const status = $("#slack-status");
    status.className = "slack-status";
    // La sesión que se está analizando: la lista de sesiones la marca.
    const info = st.analyzing ? st.analyzing_info : null;
    const analyzing = info?.session_id ?? null;
    if (state.analyzingSession !== analyzing) {
      state.analyzingSession = analyzing;
      renderSessions();
    }
    runningSince = st.analyzing ? (info?.since ?? Date.now() / 1000) : null;
    if (st.analyzing) renderRunning(status, info);
    else if (!st.enabled) status.replaceChildren();
    else if (!st.listening && !st.listener_error) status.textContent = "Arrancando…";
    else {
      const how = st.listening ? "Escuchando las notificaciones" : "Sin notificaciones";
      const backup = st.backup_poll_minutes ? ` · respaldo cada ${st.backup_poll_minutes} min` : "";
      status.textContent = `${how}${backup} · ${st.today}/${st.max_per_day} análisis hoy`;
    }
    // El error, en su recuadro: título, qué hacer y el detalle plegado. Con la vigilancia apagada no se muestra.
    showAlert($("#slack-alert"), st.enabled ? st.last_error || st.listener_error : null,
      st.enabled && st.last_error ? retryPoll : null);
    status.hidden = !status.textContent.trim();
    status.dataset.tip = st.enabled && st.last_notification ? `Última notificación: ${relTime(st.last_notification * 1000)}` : "";
    $("#slack-when").textContent = st.enabled && st.last_poll ? relTime(st.last_poll * 1000) : "";
    renderManual();
    renderLight();
  }

  // Ir a la sesión de un análisis: un botón chico con flecha, el mismo en «Analizando…» y en el resultado.
  function openButton(sessionId, label = "Ver sesión") {
    const a = document.createElement("a");
    a.className = "slack-open";
    a.href = `#/sessions/${sessionId}`;
    a.innerHTML = `<span></span><span class="slack-open-arrow" aria-hidden="true">${ICONS.chevron}</span>`;
    a.firstElementChild.textContent = label;
    return a;
  }

  // Panel plegable como el de Conexiones. Plegado queda la cabecera con el interruptor y, si la vigilancia está
  // activa, una luz que respira (del color de Slack y más rápida mientras analiza). Se recuerda en el navegador.
  const fold = $("#slack-fold");
  const FOLD_KEY = "casebook.slackPanelFolded";
  $(".conn-chevron", fold).innerHTML = ICONS.chevron;
  function setFolded(folded, remember) {
    fold.setAttribute("aria-expanded", String(!folded));
    $("#slack-body").hidden = folded;
    panel.classList.toggle("collapsed", folded);
    if (remember) {
      try {
        localStorage.setItem(FOLD_KEY, folded ? "1" : "0");
      } catch {}
    }
  }
  fold.addEventListener("click", () => setFolded(fold.getAttribute("aria-expanded") === "true", true));
  try {
    setFolded(localStorage.getItem(FOLD_KEY) === "1", false);
  } catch {}

  function renderLight() {
    const watching = !!st?.enabled;
    const analyzing = !!st?.analyzing;
    panel.classList.toggle("watching", watching);
    panel.classList.toggle("analyzing", analyzing);
    const light = $("#slack-light");
    light.dataset.tip = analyzing ? "Analizando un mensaje" : watching ? "Vigilancia activa" : "";
  }

  // «Analizando el mensaje de Ana · 1:42 · 2 en cola · Ver»: el mismo indicador que la tarjeta de la sesión.
  let runningSince = null;
  function renderRunning(status, info) {
    status.className = "slack-status slack-running";
    const orb = document.createElement("span");
    orb.className = "run-orb";
    orb.setAttribute("aria-hidden", "true");
    const what = document.createElement("span");
    what.className = "what";
    if (info?.author) {
      const who = document.createElement("strong");
      who.textContent = info.author;
      what.append(`Analizando ${info.follow_up ? "la repregunta" : "el mensaje"} de `, who);
    } else what.textContent = "Analizando un pedido";
    const clock = document.createElement("time");
    clock.className = "slack-run-clock";
    what.prepend(orb);
    const parts = [what, clock];
    if (st.queued) {
      const queue = document.createElement("span");
      queue.className = "queue";
      queue.textContent = `${st.queued} en cola`;
      parts.push(queue);
    }
    if (info?.session_id) parts.push(openButton(info.session_id));
    status.replaceChildren(...parts);
    tickRunning();
  }

  function tickRunning() {
    const clock = $("#slack-status .slack-run-clock");
    if (!clock || runningSince == null) return;
    const s = Math.max(0, Math.round(Date.now() / 1000 - runningSince));
    clock.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  setInterval(tickRunning, 1000);

  // Se reconstruye solo si cambió el error, así un «Ver detalle» abierto no se cierra con cada refresco.
  function showAlert(box, err, retry) {
    if (!err) {
      box.replaceChildren();
      return;
    }
    const alert = renderSlackError(err, { retry });
    if (box.firstElementChild?.dataset.key !== alert.dataset.key) box.replaceChildren(alert);
  }

  async function retryPoll(button) {
    button.disabled = true;
    try {
      await action("/api/slack/poll-now");
    } finally {
      setTimeout(refresh, 1500);
    }
  }

  const MANUAL = {
    searching: () => "Buscando el último mensaje…",
    queued: (m) => `Analizando el mensaje de ${m.author}…`,
    done: (m) => `Listo: análisis del mensaje de ${m.author}.`,
    failed: (m) => `El análisis del mensaje de ${m.author} falló; el detalle está en la sesión.`,
    skipped: (m) => `El mensaje de ${m.author} quedó sin analizar: se alcanzó el tope del día.`,
    existing: (m) => m.job_status === "done"
      ? `El último mensaje (de ${m.author}) ya estaba analizado.`
      : `El último mensaje (de ${m.author}) ya se está analizando.`,
  };

  function renderManual() {
    const m = st.manual;
    const el = $("#slack-manual");
    const busy = m && (m.status === "searching" || m.status === "queued");
    const btn = $("#slack-latest");
    btn.disabled = !!busy;
    btn.classList.toggle("busy", !!busy);
    $(".btn-icon", btn).innerHTML = busy ? ICONS.refresh : ICONS.spark;
    $(".label", btn).textContent = !busy ? "Analizar el último mensaje"
      : m.status === "searching" ? "Buscando el mensaje…" : "Analizando…";
    // Mientras busca, el botón ya lo dice; el resultado se muestra un rato y después queda en la lista de abajo.
    if (!m || m.status === "searching" || (!busy && Date.now() / 1000 - m.at > 600)) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    if (m.status === "error") {
      // Formato viejo: el texto venía en detail.
      showAlert(el, m.error || m.detail || "No se pudo analizar.");
      return;
    }
    el.replaceChildren();
    el.className = `slack-status${m.status === "failed" ? " fail" : ""}`;
    el.textContent = (MANUAL[m.status] || (() => ""))(m);
    // Si al mismo tiempo se está analizando otra sesión, este botón es el de la anterior; si es la misma, ya está
    // el de «Analizando…» y no se repite.
    const running = st?.analyzing ? st.analyzing_info?.session_id : null;
    if (m.session_id && m.session_id !== running) {
      el.append(openButton(m.session_id, running ? "Ver sesión anterior" : "Ver sesión"));
    }
  }

  $("#slack-latest").addEventListener("click", async () => {
    $("#slack-latest").disabled = true;
    try {
      const r = await action("/api/slack/analyze-latest");
      if (!r.ok && r.status !== 409) {
        const body = await r.json().catch(() => ({}));
        st.manual = { status: "error", at: Date.now() / 1000, error: { code: "other", title: "No se pudo empezar el análisis",
          hint: "", detail: typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`, at: Date.now() / 1000 } };
        renderManual();
        return;
      }
      await refresh();
    } finally {
      if (!st?.manual || !["searching", "queued"].includes(st.manual.status)) $("#slack-latest").disabled = false;
    }
  });

  const enableModal = $("#slack-enable-modal");
  const modalCancel = $(".modal-cancel", enableModal);
  const modalConfirm = $(".modal-confirm", enableModal);
  const modalClose = $(".modal-close", enableModal);
  const hoursInput = $("#slack-custom-hours", enableModal);
  const realtimeRadio = $('input[name="slack-lookback"][value="0"]', enableModal);
  const customRadio = $('input[name="slack-lookback"][value="custom"]', enableModal);

  function promptLookback() {
    return new Promise((resolve) => {
      const defaultMins = st?.default_lookback_minutes ?? 0;
      if (defaultMins > 0) {
        const defaultHours = Math.min(24, Math.max(1, Math.round(defaultMins / 60)));
        hoursInput.value = defaultHours;
        customRadio.checked = true;
      } else {
        realtimeRadio.checked = true;
        hoursInput.value = 1;
      }

      function onFocusHours() {
        customRadio.checked = true;
      }

      function onPresetClick(e) {
        const btn = e.target.closest(".chip-preset");
        if (!btn) return;
        hoursInput.value = btn.dataset.hours;
        customRadio.checked = true;
      }

      function cleanup() {
        modalConfirm.removeEventListener("click", onConfirm);
        modalCancel.removeEventListener("click", onCancel);
        modalClose.removeEventListener("click", onCancel);
        enableModal.removeEventListener("click", onBackdrop);
        enableModal.removeEventListener("close", onCancel);
        hoursInput.removeEventListener("focus", onFocusHours);
        hoursInput.removeEventListener("click", onFocusHours);
        enableModal.removeEventListener("click", onPresetClick);
      }

      function onConfirm() {
        cleanup();
        let minutes = 0;
        if (customRadio.checked) {
          let h = parseInt(hoursInput.value, 10);
          if (isNaN(h) || h < 1) h = 1;
          if (h > 24) h = 24;
          minutes = h * 60;
        }
        enableModal.close();
        resolve(minutes);
      }

      function onCancel() {
        cleanup();
        if (enableModal.open) enableModal.close();
        resolve(null);
      }

      function onBackdrop(e) {
        if (e.target === enableModal) onCancel();
      }

      modalConfirm.addEventListener("click", onConfirm);
      modalCancel.addEventListener("click", onCancel);
      modalClose.addEventListener("click", onCancel);
      enableModal.addEventListener("click", onBackdrop);
      enableModal.addEventListener("close", onCancel);
      hoursInput.addEventListener("focus", onFocusHours);
      hoursInput.addEventListener("click", onFocusHours);
      enableModal.addEventListener("click", onPresetClick);

      enableModal.showModal();
    });
  }

  toggle.addEventListener("click", async () => {
    const isChecked = toggle.getAttribute("aria-checked") === "true";
    if (isChecked) {
      toggle.disabled = true;
      try {
        const r = await action("/api/slack/watch/disable");
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          $("#slack-status").className = "slack-status fail";
          $("#slack-status").textContent = typeof body.detail === "string" ? body.detail : "No se pudo cambiar.";
          $("#slack-status").hidden = false;
          return;
        }
        if (typeof settingsView !== "undefined") settingsView.onExternalChange(body, ["slack.watch"]);
        await refresh();
      } finally {
        toggle.disabled = false;
      }
    } else {
      let lookback = 0;
      if (st?.ask_lookback_on_enable !== false) {
        const chosen = await promptLookback();
        if (chosen === null) return;
        lookback = chosen;
      } else {
        lookback = st?.default_lookback_minutes ?? 0;
      }

      toggle.disabled = true;
      try {
        const r = await action("/api/slack/watch/enable", { lookback_minutes: lookback });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          $("#slack-status").className = "slack-status fail";
          $("#slack-status").textContent = typeof body.detail === "string" ? body.detail : "No se pudo cambiar.";
          $("#slack-status").hidden = false;
          return;
        }
        if (typeof settingsView !== "undefined") settingsView.onExternalChange(body, ["slack.watch"]);
        await refresh();
      } finally {
        toggle.disabled = false;
      }
    }
  });

  refresh();
  // Cada 10 s, y cada 2 s mientras hay algo en curso para ver el avance.
  let tick = 0;
  setInterval(() => {
    const busy = st && (st.analyzing || ["searching", "queued"].includes(st.manual?.status));
    if (busy || ++tick % 5 === 0) refresh();
  }, 2_000);
  return { refresh };
})();

// «Elegir un mensaje…»: la ventana para escoger qué mensaje del canal analizar. Se elige el día (hoy por defecto),
// el vigilante lee sus mensajes (tarda: lo hace Claude por el conector) y cada fila se analiza, se abre o se reanaliza.
const slackPick = (() => {
  const modal = $("#slack-pick-modal");
  const body = $(".pick-body", modal);
  const foot = $(".pick-foot", modal);
  const dateInput = $(".pick-date", modal);
  const onlyRequests = $(".pick-only-input", modal);
  const dayLabelFmt = new Intl.DateTimeFormat("es", { weekday: "long", day: "numeric", month: "long" });
  const hourFmt = new Intl.DateTimeFormat("es", { hour: "2-digit", minute: "2-digit" });
  $(".pick-prev", modal).innerHTML = ICONS.chevron;
  $(".pick-next", modal).innerHTML = ICONS.chevron;
  $(".pick-refresh", modal).innerHTML = ICONS.refresh;
  let day = null; // "YYYY-MM-DD" en la hora local
  let data = null; // la última respuesta de /api/slack/day
  let timer = null;
  let confirmTs = null; // la fila que pidió confirmar «Reanalizar»

  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const parse = (s) => {
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d);
  };
  const today = () => iso(new Date());
  // El día va en el reloj de quien mira: de las 00:00 a las 00:00 del día siguiente, en epoch.
  const range = (s) => {
    const start = parse(s);
    const end = new Date(start);
    end.setDate(end.getDate() + 1);
    return { oldest: start.getTime() / 1000, latest: end.getTime() / 1000 };
  };

  function open() {
    foot.hidden = true;
    setDay(today());
    modal.showModal();
  }

  function setDay(value, { refresh = false } = {}) {
    day = value > today() ? today() : value;
    dateInput.value = day;
    dateInput.max = today();
    $(".pick-next", modal).disabled = day >= today();
    const diff = Math.round((parse(today()) - parse(day)) / 86400000);
    $(".pick-day-label", modal).textContent = diff === 0 ? "Hoy" : diff === 1 ? "Ayer" : dayLabelFmt.format(parse(day));
    data = null;
    confirmTs = null;
    load({ refresh, revalidate: true });
  }

  async function load({ refresh = false, revalidate = false } = {}) {
    clearTimeout(timer);
    const asked = day;
    if (!data || refresh) {
      foot.hidden = true; // un aviso de otro día o de una lectura anterior ya no aplica
      renderLoading();
    }
    let r;
    try {
      r = await action("/api/slack/day", { ...range(asked), refresh, revalidate });
    } catch {
      r = null;
    }
    if (asked !== day || !modal.open) return;
    if (!r || !r.ok) {
      const detail = r ? (await r.json().catch(() => ({}))).detail : "sin conexión con el visor";
      return renderError({ code: "other", title: "No pude leer los mensajes del día", hint: "",
        detail: typeof detail === "string" ? detail : `HTTP ${r?.status}`, at: Date.now() / 1000 });
    }
    data = await r.json();
    render();
    // Mientras lee, o mientras hay algo analizándose, se vuelve a preguntar (sin releer Slack).
    const busy = data.status === "searching" || data.messages.some((m) => ["queued", "analyzing"].includes(m.job?.status));
    if (busy) timer = setTimeout(() => load(), data.status === "searching" ? 2000 : 4000);
  }

  function renderLoading() {
    const box = document.createElement("div");
    box.className = "pick-loading";
    box.innerHTML = `<div class="pick-loading-head"><span class="run-orb" aria-hidden="true"></span><div>
        <strong>Leyendo los mensajes del día…</strong>
        <p>Los trae Claude por el conector de Slack: suele tardar entre 15 y 40 segundos.</p></div></div>
      <ol class="pick-ghosts" aria-hidden="true"><li></li><li></li><li></li></ol>`;
    body.replaceChildren(box);
  }

  function renderError(error) {
    const box = document.createElement("div");
    box.className = "pick-error";
    box.append(renderSlackError(error));
    const again = Object.assign(document.createElement("button"), { className: "pick-go", textContent: "Volver a intentar" });
    again.addEventListener("click", () => load({ refresh: true }));
    box.append(again);
    body.replaceChildren(box);
  }

  function render() {
    if (data.status === "searching" && !data.messages.length) return renderLoading();
    if (data.status === "error" && !data.messages.length) return renderError(data.error);
    // Si falló al volver a leer pero hay una lista anterior, se sigue mostrando y el error queda abajo.
    if (data.status === "error") {
      foot.hidden = false;
      foot.className = "pick-foot fail";
      foot.textContent = `No pude volver a leer el día (${data.error?.title || "error"}): muestro la lista anterior.`;
    }
    const all = data.messages;
    const shown = onlyRequests.checked ? all.filter((m) => m.is_request !== false) : all;
    if (!shown.length) {
      const empty = document.createElement("div");
      empty.className = "pick-empty";
      if (!all.length) empty.textContent = "Nadie escribió en el canal ese día.";
      else {
        empty.append(`Ese día hubo ${all.length} ${all.length === 1 ? "mensaje" : "mensajes"}, pero ninguno es un pedido. `);
        const showAll = Object.assign(document.createElement("button"), { className: "link-btn", textContent: "Mostrar todos" });
        showAll.addEventListener("click", () => {
          onlyRequests.checked = false;
          render();
        });
        empty.append(showAll);
      }
      return body.replaceChildren(empty);
    }
    const list = document.createElement("ol");
    list.className = "pick-list";
    list.append(...shown.map(row));
    const parts = [list];
    if (data.status === "searching") {
      const note = document.createElement("p");
      note.className = "pick-reloading";
      note.innerHTML = `<span class="run-orb" aria-hidden="true"></span>Volviendo a leer el día…`;
      parts.unshift(note);
    }
    body.replaceChildren(...parts);
  }

  function row(m) {
    const li = document.createElement("li");
    const job = m.job || {};
    const state = ["queued", "analyzing"].includes(job.status) ? "busy" : job.status === "done" ? "done" : "new";
    li.className = `pick-row ${m.is_request === false ? "is-note" : "is-request"} ${state}`;
    li.innerHTML = `<time class="pick-time"></time>
      <div class="pick-main"><div class="pick-who"><strong></strong><span class="pick-tag"></span></div>
        <p class="pick-text"></p></div>
      <div class="pick-actions"></div>`;
    $(".pick-time", li).textContent = hourFmt.format(new Date(Number(m.ts) * 1000));
    $(".pick-who strong", li).textContent = m.user_name;
    $(".pick-tag", li).textContent = m.is_request === false ? "Aviso" : "Pedido";
    if (m.files?.length) {
      const files = document.createElement("span");
      files.className = "pick-files";
      files.textContent = `${m.files.length} ${m.files.length === 1 ? "adjunto" : "adjuntos"}`;
      $(".pick-who", li).append(files);
    }
    $(".pick-text", li).textContent = m.preview || "(sin texto)";
    const actions = $(".pick-actions", li);
    if (state === "busy") {
      const busy = document.createElement("span");
      busy.className = "pick-state";
      busy.innerHTML = `<span class="run-orb" aria-hidden="true"></span>${job.status === "queued" ? "En cola…" : "Analizando…"}`;
      actions.append(busy);
      if (job.session_id) actions.append(openLink(job.session_id));
    } else if (state === "done") {
      const done = document.createElement("span");
      done.className = "pick-done";
      done.innerHTML = `${ICONS.ok}<span>Analizado</span>`;
      actions.append(done, openLink(job.session_id));
      const again = document.createElement("button");
      again.className = "link-btn pick-again";
      again.textContent = confirmTs === m.ts ? "¿Confirmas? Se vuelve a investigar" : "Reanalizar";
      again.addEventListener("click", () => {
        if (confirmTs !== m.ts) {
          confirmTs = m.ts;
          return render();
        }
        start(m, true);
      });
      actions.append(again);
    } else {
      const go = document.createElement("button");
      go.className = "pick-go";
      go.innerHTML = `${ICONS.spark}<span>${job.status === "error" || job.status === "skipped" ? "Reintentar" : "Analizar"}</span>`;
      go.addEventListener("click", () => start(m, false, go));
      actions.append(go);
    }
    return li;
  }

  function openLink(sessionId) {
    const a = document.createElement("a");
    a.className = "slack-open";
    a.href = `#/sessions/${sessionId}`;
    a.innerHTML = `<span>Ver sesión</span><span class="slack-open-arrow" aria-hidden="true">${ICONS.chevron}</span>`;
    a.addEventListener("click", () => modal.close());
    return a;
  }

  async function start(m, reanalyze, button) {
    if (button) button.disabled = true;
    confirmTs = null;
    let r;
    try {
      r = await action("/api/slack/analyze-message", { ts: m.ts, reanalyze });
    } catch {
      r = null;
    }
    const res = r ? await r.json().catch(() => ({})) : {};
    foot.hidden = false;
    foot.className = "pick-foot";
    if (!r) {
      foot.classList.add("fail");
      foot.textContent = "No se pudo empezar el análisis: sin conexión con el visor.";
      if (button) button.disabled = false;
      return;
    }
    if (!r.ok) {
      foot.classList.add("fail");
      foot.textContent = typeof res.detail === "string" ? res.detail : `No se pudo empezar el análisis (HTTP ${r.status}).`;
    } else if (res.status === "existing") {
      foot.textContent = `El mensaje de ${m.user_name} ya estaba analizado.`;
    } else if (res.status === "busy") {
      foot.textContent = `El mensaje de ${m.user_name} ya se está analizando: no se lanza otro análisis.`;
    } else {
      foot.textContent = `${reanalyze ? "Reanalizando" : "Analizando"} el mensaje de ${m.user_name}: lo puedes seguir en vivo en su sesión `
        + "y en el panel de Slack.";
    }
    slackPanel.refresh();
    load();
  }

  $("#slack-pick").addEventListener("click", open);
  $(".modal-close", modal).addEventListener("click", () => modal.close());
  modal.addEventListener("close", () => clearTimeout(timer));
  modal.addEventListener("click", (e) => e.target === modal && modal.close()); // clic en el fondo
  $(".pick-prev", modal).addEventListener("click", () => {
    const d = parse(day);
    d.setDate(d.getDate() - 1);
    setDay(iso(d));
  });
  $(".pick-next", modal).addEventListener("click", () => {
    const d = parse(day);
    d.setDate(d.getDate() + 1);
    setDay(iso(d));
  });
  dateInput.addEventListener("change", () => dateInput.value && setDay(dateInput.value));
  onlyRequests.addEventListener("change", () => data && render());
  $(".pick-refresh", modal).addEventListener("click", () => setDay(day, { refresh: true }));
  return { open };
})();
