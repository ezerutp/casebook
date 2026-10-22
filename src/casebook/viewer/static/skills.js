// Skills: las del repo y cómo están instaladas en ~/.claude/skills, más las demás del usuario (solo lectura).
// Usa lo que define app.js: $, ICONS, svg, action, esc, relTime, flash.

const SKILL_STATES = {
  linked: { label: "enlazada al repo", cls: "ok", text: "Instalada como link al repo: siempre está al día con lo que hay en skills/." },
  missing: { label: "no instalada", cls: "none", text: "No está en ~/.claude/skills: Claude no la carga." },
  broken: { label: "link roto", cls: "fail", text: "El link apunta a una carpeta del repo que ya no existe (¿se movió el repo?). Reparar lo vuelve a crear." },
  foreign_link: { label: "apunta a otro lado", cls: "warn", text: "Hay algo con ese nombre que no es de este repo. No se toca: quítalo a mano si quieres usar la del repo." },
  copy: { label: "copia", cls: "warn", text: "Está instalada como carpeta copiada, no como link: no se actualiza con el repo. No se toca: bórrala a mano y vuelve a instalarla." },
};
const SERVER_ITEM = "__server__";

const skillsView = (() => {
  const root = $("#skills-view");
  let data = null;
  let selected = null;
  let tab = "preview";
  let detailCache = new Map();

  async function load() {
    const r = await fetch("/api/skills");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    data = await r.json();
    detailCache = new Map();
    paintBadge();
  }

  async function init() {
    try {
      await load();
    } catch {
      // servidor de una versión anterior: sin aviso
    }
  }

  function paintBadge() {
    const bad = data.repo.filter((s) => s.status !== "linked").length;
    const badge = $("#skills-badge");
    badge.hidden = !bad;
    badge.textContent = bad;
    badge.title = bad ? `${bad} skill${bad > 1 ? "s" : ""} del repo sin instalar bien` : "";
  }

  async function show() {
    if (!data) {
      try {
        await load();
      } catch {
        $(".skill-detail", root).innerHTML = `<p class="empty">No se pudieron leer las skills. Reinicia Claude Code para tomar la versión nueva del servidor.</p>`;
        return;
      }
    }
    const fromHash = decodeURIComponent(location.hash.split("/")[2] || "");
    const next = fromHash || selected || data.repo[0]?.name || SERVER_ITEM;
    if (next !== selected) tab = "preview";
    selected = next;
    renderList();
    await renderDetail();
  }

  // --- lista --------------------------------------------------------------------------------------------

  function renderList() {
    $(".skills-intro", root).innerHTML = `Las del repo se instalan como link en <code>${esc(data.user_dir)}</code>. Claude Code las carga al abrir una sesión nueva.`;
    const q = $(".skills-filter", root).value.trim().toLowerCase();
    const match = (s) => !q || s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q);
    const groups = [
      ["Del repo", data.repo.filter(match)],
      ["Con el servidor MCP", match({ name: "instrucciones del servidor", description: data.server_instructions }) ? [null] : []],
      ["Otras skills tuyas", data.others.filter(match)],
    ];
    const box = $(".skills-groups", root);
    box.replaceChildren(
      ...groups
        .filter(([, items]) => items.length)
        .map(([title, items]) => {
          const g = document.createElement("section");
          g.className = "skills-group";
          g.innerHTML = `<h3></h3>`;
          $("h3", g).textContent = title;
          items.forEach((s) => g.append(item(s)));
          return g;
        })
    );
    if (!box.children.length) box.innerHTML = `<p class="empty">Ninguna skill coincide.</p>`;
  }

  function item(s) {
    const a = document.createElement("a");
    const key = s ? s.name : SERVER_ITEM;
    a.className = "skill-item";
    a.href = `#/skills/${encodeURIComponent(key)}`;
    if (key === selected) a.setAttribute("aria-current", "true");
    a.innerHTML = `<span class="skill-top"><span class="btn-icon">${ICONS.skills}</span><span class="skill-name"></span><span class="state-tag"></span></span>
      <span class="skill-desc"></span><span class="skill-meta"></span>`;
    const tag = $(".state-tag", a);
    if (!s) {
      $(".skill-name", a).textContent = "instrucciones del servidor";
      $(".skill-desc", a).textContent = "Lo básico que recibe Claude al conectarse: session_start, purpose, cuidados por sistema.";
      $(".skill-meta", a).textContent = "server.py · se actualiza solo";
      tag.className = "state-tag none";
      tag.textContent = "con el servidor";
      return a;
    }
    $(".skill-name", a).textContent = s.name;
    $(".skill-desc", a).textContent = s.description || "Sin descripción.";
    if (s.source === "repo") {
      const st = stateOf(s);
      tag.className = `state-tag ${st.cls}`;
      tag.textContent = st.label;
      $(".skill-meta", a).textContent = `skills/ · ${s.files.length} archivo${s.files.length > 1 ? "s" : ""}`;
    } else {
      tag.className = "state-tag none";
      tag.textContent = "solo lectura";
      $(".skill-meta", a).textContent = s.link_target ? `→ ${s.link_target}` : "carpeta";
    }
    return a;
  }

  function stateOf(s) {
    const st = { ...SKILL_STATES[s.status] };
    if (s.status === "copy") st.label = s.same_as_repo ? "copia al día" : "copia desactualizada";
    return st;
  }

  $(".skills-filter", root).addEventListener("input", renderList);
  $(".skills-reload", root).addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      await load();
      renderList();
      await renderDetail();
      flash(btn, "Al día");
    } finally {
      btn.disabled = false;
    }
  });

  // --- detalle ------------------------------------------------------------------------------------------

  async function renderDetail() {
    const box = $(".skill-detail", root);
    if (selected === SERVER_ITEM) return renderServer(box);
    const s = data.repo.find((x) => x.name === selected) || data.others.find((x) => x.name === selected);
    if (!s) {
      box.innerHTML = `<p class="empty">No hay una skill llamada <code>${esc(selected)}</code>.</p>`;
      return;
    }
    let d = detailCache.get(s.name);
    if (!d) {
      const r = await fetch(`/api/skills/${encodeURIComponent(s.name)}`);
      d = r.ok ? await r.json() : { skill_md: "", diff: null, truncated: false };
      detailCache.set(s.name, d);
    }
    if (tab === "diff" && !d.diff) tab = "preview";

    const isRepo = s.source === "repo";
    const st = isRepo ? stateOf(s) : null;
    box.innerHTML = `<header class="skill-header">
        <div class="skill-title"><h2></h2><span class="state-tag"></span></div>
        <p class="skill-state-text"></p>
        <div class="skill-actions"></div>
        <p class="skill-error" hidden></p>
      </header>
      <dl class="skill-meta-list"></dl>
      <div class="skill-tabs" role="tablist"></div>
      <div class="skill-body"></div>
      <p class="skill-note"><span class="btn-icon">${ICONS.warn}</span><span>Los cambios se ven en sesiones nuevas de Claude Code; las abiertas siguen con lo que cargaron al empezar.</span></p>`;
    $("h2", box).textContent = s.name;
    const tag = $(".skill-title .state-tag", box);
    tag.className = `state-tag ${isRepo ? st.cls : "none"}`;
    tag.textContent = isRepo ? st.label : "solo lectura";
    $(".skill-state-text", box).textContent = isRepo
      ? st.text
      : "No es de este repo: se muestra para tenerla a mano, pero el visor no la instala ni la quita.";

    const actions = $(".skill-actions", box);
    if (isRepo && s.can_install) actions.append(actionBtn(s.status === "missing" ? "Instalar" : "Reparar link", "primary", "link", "install", s));
    if (isRepo && s.can_uninstall) actions.append(actionBtn("Desinstalar", "danger", "trash", "uninstall", s));
    actions.append(copyBtn("Copiar ruta", isRepo ? s.installed_path : s.path));

    const meta = [
      ["Descripción", s.description || "—", false],
      ["Tamaño", `${fmtBytes(s.bytes)} · ${s.lines} líneas de SKILL.md · ~${fmtK(s.tokens)} tokens`, false],
      [isRepo ? "Último cambio en el repo" : "Última modificación", s.modified ? `${relTime(s.modified)} (${new Date(s.modified).toLocaleString()})` : "—", false],
    ];
    if (isRepo) {
      meta.push(["En el repo", s.path, true], ["Instalada en", s.installed_path, true]);
      if (s.link_target) meta.push(["El link apunta a", s.link_target, true]);
    } else {
      meta.push(["Carpeta", s.path, true]);
      if (s.link_target) meta.push(["El link apunta a", s.link_target, true]);
    }
    if (s.declared_name && s.declared_name !== s.name) meta.push(["name en SKILL.md", `${s.declared_name} (no coincide con la carpeta)`, true]);
    $(".skill-meta-list", box).replaceChildren(
      ...meta.flatMap(([k, v, mono]) => {
        const dt = document.createElement("dt");
        dt.textContent = k;
        const dd = document.createElement("dd");
        dd.textContent = v;
        if (mono) dd.className = "mono";
        return [dt, dd];
      })
    );

    const tabs = [["preview", "Vista previa"], ...(d.diff ? [["diff", "Cambios contra el repo"]] : []), ["files", `Archivos (${s.files.length})`]];
    $(".skill-tabs", box).replaceChildren(
      ...tabs.map(([id, label]) => {
        const b = document.createElement("button");
        b.setAttribute("role", "tab");
        b.setAttribute("aria-selected", String(tab === id));
        b.textContent = label;
        b.addEventListener("click", () => {
          tab = id;
          renderDetail();
        });
        return b;
      })
    );
    const body = $(".skill-body", box);
    if (tab === "diff") body.append(diffView(d.diff));
    else if (tab === "files") body.append(filesView(s.files));
    else body.append(markdown(d.skill_md, d.truncated));
  }

  function renderServer(box) {
    box.innerHTML = `<header class="skill-header">
        <div class="skill-title"><h2>instrucciones del servidor</h2><span class="state-tag none">con el servidor</span></div>
        <p class="skill-state-text">Claude las recibe al conectarse al servidor MCP, en todas las sesiones. Salen de <code>INSTRUCTIONS</code> en server.py, con los textos del pack, y se actualizan al reiniciar Claude Code.</p>
      </header>
      <div class="skill-body"></div>`;
    const pre = document.createElement("pre");
    pre.className = "md-pre";
    pre.textContent = data.server_instructions;
    $(".skill-body", box).append(pre);
  }

  function actionBtn(label, kind, icon, verb, s) {
    const b = document.createElement("button");
    b.className = kind;
    b.innerHTML = `<span class="btn-icon">${ICONS[icon]}</span><span></span>`;
    b.lastChild.textContent = label;
    b.addEventListener("click", async () => {
      b.disabled = true;
      const err = $(".skill-error", root);
      err.hidden = true;
      try {
        const r = await action(`/api/skills/${encodeURIComponent(s.name)}/${verb}`);
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          err.hidden = false;
          err.textContent = body.detail || "No se pudo.";
          return;
        }
        await load();
        renderList();
        await renderDetail();
      } finally {
        b.disabled = false;
      }
    });
    return b;
  }

  function copyBtn(label, text) {
    const b = document.createElement("button");
    b.innerHTML = `<span class="btn-icon">${ICONS.copy}</span><span></span>`;
    b.lastChild.textContent = label;
    b.addEventListener("click", async () => {
      await navigator.clipboard.writeText(text);
      flash(b, "Copiada");
    });
    return b;
  }

  function fmtBytes(n) {
    return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
  }

  function fmtK(n) {
    return n < 1000 ? String(n) : `${(n / 1000).toFixed(1)} k`;
  }

  function filesView(files) {
    const ul = document.createElement("ul");
    ul.className = "skill-files";
    files.forEach((f) => {
      const li = document.createElement("li");
      li.textContent = f;
      ul.append(li);
    });
    return ul;
  }

  function diffView(diff) {
    const pre = document.createElement("pre");
    pre.className = "diff";
    diff.split("\n").forEach((line) => {
      const span = document.createElement("span");
      span.textContent = line + "\n";
      if (line.startsWith("+") && !line.startsWith("+++")) span.className = "add";
      else if (line.startsWith("-") && !line.startsWith("---")) span.className = "del";
      else if (line.startsWith("@@")) span.className = "hunk";
      pre.append(span);
    });
    return pre;
  }

  // Markdown mínimo para la vista previa: títulos, listas, bloques de código, párrafos y `código` / **negrita**.
  // Todo pasa por textContent o esc(): el contenido nunca se interpreta como HTML.
  function markdown(text, truncated) {
    const box = document.createElement("div");
    box.className = "md";
    const body = text.replace(/^---\n[\s\S]*?\n---\n?/, "");
    const lines = body.split("\n");
    let i = 0;
    let list = null;
    let para = [];
    const inline = (s) =>
      esc(s)
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1");
    const flushPara = () => {
      if (para.length) {
        const p = document.createElement("p");
        p.innerHTML = inline(para.join(" "));
        box.append(p);
        para = [];
      }
    };
    const closeList = () => (list = null);
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*```/);
      if (fence) {
        flushPara();
        closeList();
        const code = [];
        i += 1;
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
        const pre = document.createElement("pre");
        pre.className = "md-pre";
        pre.textContent = code.join("\n");
        box.append(pre);
        i += 1;
        continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      const li = line.match(/^(\s*)([-*]|\d+\.)\s+(.*)$/);
      if (h) {
        flushPara();
        closeList();
        const el = document.createElement(`h${Math.min(h[1].length + 2, 6)}`);
        el.innerHTML = inline(h[2]);
        box.append(el);
      } else if (li) {
        flushPara();
        const ordered = /\d/.test(li[2]);
        if (!list || list.ordered !== ordered) {
          list = { ordered, el: document.createElement(ordered ? "ol" : "ul") };
          box.append(list.el);
        }
        const item = document.createElement("li");
        item.innerHTML = inline(li[3]);
        if (li[1].length >= 2) item.className = "nested";
        list.el.append(item);
      } else if (!line.trim()) {
        flushPara();
        closeList();
      } else if (list && /^\s{2,}\S/.test(line)) {
        // continuación de un ítem
        const last = list.el.lastElementChild;
        last.innerHTML += " " + inline(line.trim());
      } else {
        closeList();
        para.push(line.trim());
      }
      i += 1;
    }
    flushPara();
    if (truncated) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "(recortado: el archivo es muy grande para la vista previa)";
      box.append(p);
    }
    return box;
  }

  return { init, show };
})();
