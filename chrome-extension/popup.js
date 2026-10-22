// Muestra lo que el native host validó por servicio (sin valores de cookies) y permite reenviar las sesiones.
const HOST = "casebook.native_host";

function ago(seconds) {
  const minutes = Math.floor((Date.now() / 1000 - seconds) / 60);
  if (minutes < 1) return "hace menos de un minuto";
  if (minutes < 120) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `hace ${hours} h` : `hace ${Math.floor(hours / 24)} días`;
}

let lastPush = {}; // lo que respondió «Enviar sesiones ahora», por servicio
let missingOrigins = []; // dominios de las fuentes sin permiso todavía (los pide «Dar permiso»)

function row(system, label, result) {
  const li = document.createElement("li");
  li.className = result ? (result.ok ? "ok" : "fail") : "";
  const dot = document.createElement("span");
  dot.className = "dot";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = label || system;
  const detail = document.createElement("span");
  detail.className = "detail";
  if (!result) detail.textContent = "Todavía no se entregó ninguna sesión.";
  else if (result.ok) detail.textContent = `Sesión guardada ${ago(result.at)}.`;
  else detail.textContent = `No validó ${ago(result.at)}: ${result.detail}`;
  li.append(dot, name, detail);
  const pushed = lastPush[system];
  if (pushed && !pushed.ok) {
    const now = document.createElement("span");
    now.className = "detail now";
    now.textContent = `Ahora: ${pushed.detail}.`;
    li.append(now);
  }
  return li;
}

// Los dominios de las fuentes (los dice el native host, según el pack) para los que Chrome todavía no dio permiso.
async function checkPermissions() {
  const perms = document.getElementById("perms");
  try {
    const { targets = [] } = await chrome.runtime.sendNativeMessage(HOST, { type: "targets" });
    const origins = [...new Set(targets.flatMap((t) => t.origins || []))];
    const granted = await Promise.all(origins.map((o) => chrome.permissions.contains({ origins: [o] })));
    missingOrigins = origins.filter((_, i) => !granted[i]);
  } catch {
    missingOrigins = [];
  }
  perms.hidden = !missingOrigins.length;
  document.getElementById("perms-hosts").textContent =
    missingOrigins.map((o) => o.replace(/^https:\/\//, "").replace(/\/\*$/, "")).join(", ");
}

async function render() {
  const list = document.getElementById("systems");
  const error = document.getElementById("error");
  await checkPermissions();
  try {
    const status = await chrome.runtime.sendNativeMessage(HOST, { type: "status" });
    error.hidden = true;
    // Los sistemas y sus nombres salen de config.toml (vía el native host); un host viejo no manda labels.
    const labels = status.labels || Object.fromEntries(Object.keys(status.systems || {}).map((s) => [s, s]));
    list.replaceChildren(...Object.entries(labels).map(([s, label]) => row(s, label, (status.systems || {})[s])));
  } catch {
    list.replaceChildren();
    error.hidden = false;
    error.innerHTML = "No encuentro el programa local. Corre <code>casebook extension</code> " +
      "en tu terminal y vuelve a abrir esta ventana.";
  }
}

// chrome.permissions.request tiene que llamarse directo desde el clic (sin await antes).
document.getElementById("grant").addEventListener("click", () => {
  chrome.permissions.request({ origins: missingOrigins }).then((ok) => {
    if (ok) chrome.runtime.sendMessage({ type: "push-now" }).then((reply) => {
      lastPush = (reply && reply.results) || {};
      render();
    });
    else render();
  });
});

document.getElementById("push").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Enviando…";
  try {
    const reply = await chrome.runtime.sendMessage({ type: "push-now" });
    lastPush = (reply && reply.results) || {};
  } finally {
    button.disabled = false;
    button.textContent = "Enviar sesiones ahora";
    render();
  }
});

render();
