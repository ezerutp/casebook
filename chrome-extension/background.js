// Cuando cambian las cookies de un servicio (o termina de cargar su página), manda al native host las cookies
// que Chrome enviaría a la URL de validación. El host las valida y las guarda en el keyring; esta extensión no
// guarda ni registra valores.
//
// La extensión no trae dominios fijos: el native host dice cuáles usa cada sistema (salen del pack del equipo) y
// el popup pide permiso para ellos. Sin ese permiso, Chrome no deja leer sus cookies ni avisa cuando cambian.
const HOST = "casebook.native_host";
const DEBOUNCE_MS = 3000;
const MIN_INTERVAL_MS = 15000; // OpenSearch rota cookies en cada respuesta: como mucho un envío cada 15 s
const RETRY_TARGETS_MS = 60000;

let targets = null; // [{system, app_host, cookie_url, origins}], los entrega el host según la configuración
let targetsFailedAt = 0;
const timers = {};
const lastPush = {};

async function getTargets() {
  if (targets) return targets;
  if (Date.now() - targetsFailedAt < RETRY_TARGETS_MS) return [];
  try {
    const reply = await chrome.runtime.sendNativeMessage(HOST, { type: "targets" });
    targets = reply.targets || [];
  } catch (err) {
    targetsFailedAt = Date.now();
    console.warn("casebook: no hay native host; corre `casebook extension`", err.message);
    return [];
  }
  return targets;
}

function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

function appliesTo(cookieDomain, host) {
  const domain = cookieDomain.replace(/^\./, "");
  return host === domain || host.endsWith("." + domain);
}

async function allowed(target) {
  const origins = target.origins || [`https://${hostOf(target.cookie_url)}/*`];
  try {
    return await chrome.permissions.contains({ origins });
  } catch {
    return false;
  }
}

// Devuelve qué pasó, para el popup. Solo se envían las cookies que Chrome mandaría a cookie_url.
async function push(system) {
  const target = (await getTargets()).find((t) => t.system === system);
  if (!target) return { ok: false, detail: "no hay native host" };
  if (!(await allowed(target))) return { ok: false, detail: `falta dar permiso a ${target.app_host}: ábrelo desde el popup` };
  const cookies = await chrome.cookies.getAll({ url: target.cookie_url });
  if (!cookies.length) return { ok: false, detail: `este perfil no tiene sesión en ${target.app_host}` };
  lastPush[system] = Date.now();
  const cookie = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  try {
    const reply = await chrome.runtime.sendNativeMessage(HOST, { type: "cookies", system, cookie });
    console.info(`casebook: ${system}: ${reply.detail}`);
    return reply;
  } catch (err) {
    console.warn(`casebook: ${system}: no se pudo entregar la sesión`, err.message);
    return { ok: false, detail: "no se pudo hablar con el native host" };
  }
}

function schedule(system) {
  clearTimeout(timers[system]);
  const wait = Math.max(DEBOUNCE_MS, (lastPush[system] || 0) + MIN_INTERVAL_MS - Date.now());
  timers[system] = setTimeout(() => push(system), wait);
}

chrome.cookies.onChanged.addListener(async ({ cookie, removed }) => {
  if (removed) return; // un borrado o vencimiento no trae nada que entregar
  for (const t of await getTargets()) {
    if (appliesTo(cookie.domain, hostOf(t.cookie_url))) schedule(t.system);
  }
});

// Con la sesión ya iniciada no cambia ninguna cookie: abrir la página (lo hace `casebook login`) basta.
chrome.tabs.onUpdated.addListener(async (_tabId, info, tab) => {
  if (info.status !== "complete" || !tab.url) return;
  const host = hostOf(tab.url);
  for (const t of await getTargets()) {
    if (t.app_host === host) schedule(t.system);
  }
});

async function pushAll() {
  const all = await getTargets();
  for (const t of all) schedule(t.system);
  await reportPermissions(all);
}

// Le cuenta al host qué dominios faltan, para que `doctor` y el instalador avisen que hay que abrir el popup.
async function reportPermissions(all) {
  const origins = [...new Set(all.flatMap((t) => t.origins || []))];
  const missing = [];
  for (const origin of origins) {
    if (!(await chrome.permissions.contains({ origins: [origin] }).catch(() => false))) missing.push(origin);
  }
  try {
    await chrome.runtime.sendNativeMessage(HOST, { type: "permissions", missing });
  } catch {
    // sin native host no hay a quién avisarle
  }
}

chrome.runtime.onStartup.addListener(pushAll);
chrome.runtime.onInstalled.addListener(pushAll);
// Al dar permiso desde el popup: entregar ya las sesiones que estaban esperando.
chrome.permissions.onAdded.addListener(() => {
  targets = null;
  targetsFailedAt = 0;
  pushAll();
});

// «Enviar sesiones ahora» del popup: sin esperar el intervalo mínimo; responde cuando el host contestó.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "push-now") return false;
  targets = null; // por si el host no estaba registrado cuando arrancó el service worker
  targetsFailedAt = 0;
  getTargets()
    .then((all) => Promise.all(all.map(async (t) => {
      clearTimeout(timers[t.system]);
      return [t.system, await push(t.system)];
    })))
    .then((results) => sendResponse({ results: Object.fromEntries(results) }))
    .catch((err) => sendResponse({ error: err.message }));
  return true; // respuesta asíncrona
});
