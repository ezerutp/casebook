// Botón flotante «Ir arriba». Aparece cuando ya se bajó bastante. Se puede arrastrar: al soltarlo se pega al
// borde (izquierdo del contenido o derecho de la ventana) más cercano y recuerda la altura en este navegador,
// para que el ingeniero lo deje donde no tape nada. Hasta que se arrastra por primera vez, al aparecer hace una
// pista de que se puede mover: se balancea y muestra flechitas arriba y abajo.
(() => {
  const KEY = "casebook.scrollTop";
  const DRAGGED_KEY = "casebook.scrollTop.dragged";
  const MARGIN = 16;
  const DRAG_THRESHOLD = 5;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const btn = document.createElement("button");
  btn.className = "scroll-top";
  btn.type = "button";
  btn.dataset.tip = "Ir arriba\nArrástralo para moverlo · doble clic para volver a su lugar";
  btn.dataset.tipPlace = "left";
  btn.setAttribute("aria-label", "Ir arriba");
  btn.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M6 11l6-6 6 6"/></svg>'
    + '<span class="scroll-top-grip up" aria-hidden="true"></span><span class="scroll-top-grip down" aria-hidden="true"></span>';
  document.body.appendChild(btn);

  // Posición guardada: lado y altura como fracción de la ventana, para que sobreviva a cambios de tamaño.
  let pos = { side: "right", y: 1 };
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "null");
    if (saved && (saved.side === "left" || saved.side === "right") && typeof saved.y === "number") pos = saved;
  } catch {}

  const contentLeft = () => {
    const main = document.querySelector("main");
    return main ? Math.max(0, main.getBoundingClientRect().left) : 0;
  };

  function place() {
    const size = btn.offsetWidth || 40;
    const maxY = window.innerHeight - size - MARGIN;
    const top = Math.min(maxY, Math.max(MARGIN, MARGIN + pos.y * (maxY - MARGIN)));
    const left = pos.side === "left" ? contentLeft() + MARGIN : window.innerWidth - size - MARGIN;
    btn.style.left = `${left}px`;
    btn.style.top = `${top}px`;
  }

  let dragged = false;
  try { dragged = localStorage.getItem(DRAGGED_KEY) === "1"; } catch {}
  let hinted = false; // la pista sale una vez por carga de página, y nunca más después del primer arrastre

  function updateVisibility() {
    const show = window.scrollY > window.innerHeight * 0.6;
    const appearing = show && !btn.classList.contains("visible");
    btn.classList.toggle("visible", show);
    if (appearing && !dragged && !hinted && !reduceMotion.matches) {
      hinted = true;
      btn.classList.add("hint");
    }
  }
  btn.addEventListener("animationend", (e) => { if (e.target === btn) btn.classList.remove("hint"); });

  // Arrastre con pointer events: si se mueve menos que el umbral, cuenta como clic.
  let drag = null;
  btn.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const r = btn.getBoundingClientRect();
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, dx: e.clientX - r.left, dy: e.clientY - r.top, moved: false };
    btn.setPointerCapture(e.pointerId);
  });

  btn.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < DRAG_THRESHOLD) return;
    drag.moved = true;
    btn.classList.remove("hint");
    btn.classList.add("dragging");
    const size = btn.offsetWidth;
    const left = Math.min(window.innerWidth - size, Math.max(0, e.clientX - drag.dx));
    const top = Math.min(window.innerHeight - size, Math.max(0, e.clientY - drag.dy));
    btn.style.left = `${left}px`;
    btn.style.top = `${top}px`;
  });

  function endDrag(e) {
    if (!drag || e.pointerId !== drag.id) return;
    const moved = drag.moved;
    drag = null;
    btn.classList.remove("dragging");
    if (!moved) return;
    const r = btn.getBoundingClientRect();
    const center = r.left + r.width / 2;
    const leftEdge = contentLeft();
    const side = center - leftEdge < window.innerWidth - center ? "left" : "right";
    const maxY = window.innerHeight - r.height - MARGIN;
    const y = maxY > MARGIN ? Math.min(1, Math.max(0, (r.top - MARGIN) / (maxY - MARGIN))) : 1;
    pos = { side, y };
    dragged = true;
    try {
      localStorage.setItem(KEY, JSON.stringify(pos));
      localStorage.setItem(DRAGGED_KEY, "1");
    } catch {}
    place();
    btn.dataset.justDragged = "1";
  }
  btn.addEventListener("pointerup", endDrag);
  btn.addEventListener("pointercancel", endDrag);

  btn.addEventListener("click", (e) => {
    if (btn.dataset.justDragged) {
      delete btn.dataset.justDragged;
      e.preventDefault();
      return;
    }
    window.scrollTo({ top: 0, behavior: reduceMotion.matches ? "auto" : "smooth" });
  });

  // Doble clic: vuelve a la esquina inferior derecha.
  btn.addEventListener("dblclick", () => {
    pos = { side: "right", y: 1 };
    try { localStorage.removeItem(KEY); } catch {}
    place();
  });

  window.addEventListener("scroll", updateVisibility, { passive: true });
  window.addEventListener("resize", place);
  place();
  updateVisibility();
})();
