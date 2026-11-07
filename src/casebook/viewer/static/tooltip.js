// Tooltip propio del visor: no se usa `title` nativo. data-tip son líneas separadas por saltos de línea;
// data-tip-title, si está, va arriba en negrita (con data-tip-status como etiqueta de estado). Aparece al pasar
// el mouse o al enfocar con teclado; un data-tip vacío no muestra nada.
(() => {
  const GAP = 8;
  const DELAY = 250;
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.setAttribute("role", "tooltip");
  tip.id = "tip";
  document.body.appendChild(tip);

  let current = null;
  let timer = null;

  function fill(el) {
    tip.replaceChildren();
    const title = el.dataset.tipTitle;
    if (title) {
      const h = document.createElement("div");
      h.className = "tip-title";
      h.textContent = title;
      if (el.dataset.tipStatus) {
        const s = document.createElement("span");
        s.className = `tip-status ${el.dataset.tipStatus}`;
        s.textContent = el.dataset.tipStatusLabel || el.dataset.tipStatus;
        h.append(s);
      }
      tip.append(h);
    }
    const lines = (el.dataset.tip || "").split("\n").filter(Boolean);
    if (lines.length) {
      const ul = document.createElement("ul");
      for (const line of lines) {
        const li = document.createElement("li");
        li.textContent = line;
        ul.append(li);
      }
      tip.append(ul);
    }
  }

  // Lado preferido con data-tip-place (top, bottom, right, left; top por defecto). Si no cabe se prueba el
  // opuesto y luego los demás; al final se ajusta para no salirse de la ventana.
  const ORDER = {
    top: ["top", "bottom", "right", "left"], bottom: ["bottom", "top", "right", "left"],
    right: ["right", "left", "bottom", "top"], left: ["left", "right", "bottom", "top"],
  };
  function place(el) {
    const r = el.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    const at = {
      top: [r.left + r.width / 2 - t.width / 2, r.top - GAP - t.height],
      bottom: [r.left + r.width / 2 - t.width / 2, r.bottom + GAP],
      right: [r.right + GAP, r.top + r.height / 2 - t.height / 2],
      left: [r.left - GAP - t.width, r.top + r.height / 2 - t.height / 2],
    };
    const fits = {
      top: r.top - GAP - t.height >= GAP, bottom: r.bottom + GAP + t.height <= vh - GAP,
      right: r.right + GAP + t.width <= vw - GAP, left: r.left - GAP - t.width >= GAP,
    };
    const order = ORDER[el.dataset.tipPlace] || ORDER.top;
    const side = order.find((s) => fits[s]) || order[0];
    let [left, top] = at[side];
    left = Math.min(Math.max(GAP, left), vw - t.width - GAP);
    top = Math.min(Math.max(GAP, top), vh - t.height - GAP);
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    tip.dataset.side = side;
    // La flecha apunta al centro del elemento aunque el tooltip se haya corrido para caber en la ventana.
    const vertical = side === "top" || side === "bottom";
    const arrow = vertical
      ? Math.min(Math.max(10, r.left + r.width / 2 - left), t.width - 10)
      : Math.min(Math.max(10, r.top + r.height / 2 - top), t.height - 10);
    tip.style.setProperty("--arrow", `${arrow}px`);
  }

  const hasContent = (el) => Boolean(el.dataset.tipTitle || el.dataset.tip?.trim());

  function show(el) {
    if (!hasContent(el) || !el.isConnected) return hide();
    current = el;
    fill(el);
    tip.classList.add("measuring");
    place(el);
    tip.classList.remove("measuring");
    tip.classList.add("open");
    el.setAttribute("aria-describedby", "tip");
  }

  function hide() {
    clearTimeout(timer);
    if (current) current.removeAttribute("aria-describedby");
    current = null;
    tip.classList.remove("open");
  }

  function enter(el, immediate) {
    clearTimeout(timer);
    if (current === el) return;
    // Si ya hay uno abierto se pasa al siguiente sin esperar, como al recorrer una lista.
    if (immediate || current) show(el);
    else timer = setTimeout(() => show(el), DELAY);
  }

  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest?.("[data-tip], [data-tip-title]");
    if (el) enter(el, false);
  });
  document.addEventListener("mouseout", (e) => {
    const el = e.target.closest?.("[data-tip], [data-tip-title]");
    if (el && !el.contains(e.relatedTarget)) hide();
  });
  document.addEventListener("focusin", (e) => {
    // Solo con foco de teclado: el foco que deja un clic no debe reabrirlo.
    const el = e.target.closest?.("[data-tip], [data-tip-title]");
    if (el && e.target.matches(":focus-visible")) enter(el, true);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
  // Al hacer clic (o empezar a arrastrar) el tooltip estorba: se cierra hasta que el mouse salga y vuelva.
  document.addEventListener("pointerdown", hide, true);
  // Solo cierra el scroll que mueve al elemento (la página o un contenedor suyo), no el de otra lista.
  window.addEventListener("scroll", (e) => {
    const box = e.target === document ? document.documentElement : e.target;
    if (current && box.contains?.(current)) hide();
  }, { passive: true, capture: true });
  window.addEventListener("resize", hide);
})();
