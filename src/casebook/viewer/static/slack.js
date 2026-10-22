// Panel de Slack en la barra lateral: toggle de la vigilancia (slack.watch en config.toml), estado del vigilante y
// el análisis manual del último mensaje. El análisis de cada pedido queda como una sesión normal; aquí solo se
// listan los últimos.

const slackPanel = (() => {
  const panel = $("#slack-panel");
  const toggle = $("#slack-toggle");
  // Las etiquetas de los resultados del triage llegan en /api/slack (las define el pack); estas son las propias.
  const STATES = { skipped: "sin analizar (tope)", error: "falló" };
  const label = (outcome) => STATES[outcome] || st?.outcomes?.[outcome] || outcome;
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
    const status = $("#slack-status");
    status.className = "slack-status";
    if (st.analyzing) status.textContent = `Analizando un pedido…${st.queued ? ` (${st.queued} en cola)` : ""}`;
    else if (!st.enabled) status.textContent = "Vigilancia apagada. Nunca escribe en Slack.";
    else if (!st.listening && !st.listener_error) status.textContent = "Arrancando…";
    else {
      const how = st.listening ? "Escuchando las notificaciones" : "Sin notificaciones";
      const backup = st.backup_poll_minutes ? ` · respaldo cada ${st.backup_poll_minutes} min` : "";
      status.textContent = `${how}${backup} · ${st.today}/${st.max_per_day} análisis hoy`;
    }
    // El error, en su recuadro: título, qué hacer y el detalle plegado. Con la vigilancia apagada no se muestra.
    showAlert($("#slack-alert"), st.enabled ? st.last_error || st.listener_error : null,
      st.enabled && st.last_error ? retryPoll : null);
    status.title = st.enabled && st.last_notification ? `Última notificación: ${relTime(st.last_notification * 1000)}` : "";
    $("#slack-when").textContent = st.enabled && st.last_poll ? relTime(st.last_poll * 1000) : "";
    renderManual();
    $("#slack-recent").replaceChildren(
      ...st.recent.slice(0, 5).map((e) => {
        const li = document.createElement("li");
        li.className = [e.outcome || "", e.outcome && e.outcome === st?.escalation ? "escalation" : ""].join(" ").trim();
        li.innerHTML = `<a><span class="who"></span><span class="what"></span></a>`;
        $("a", li).href = `#/sessions/${e.session_id}`;
        $(".who", li).textContent = `${e.follow_up ? "↩ " : ""}${e.author}`;
        $(".what", li).textContent = e.outcome ? label(e.outcome) : "analizando…";
        li.title = relTime(e.at * 1000);
        return li;
      })
    );
  }

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
    if (m.session_id) {
      const a = document.createElement("a");
      a.href = `#/sessions/${m.session_id}`;
      a.textContent = " Ver sesión";
      el.append(a);
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
