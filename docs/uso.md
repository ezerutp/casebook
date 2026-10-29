# Uso del visor y las sesiones

Pídele a Claude que investigue una card (por ejemplo "investiga SUP-1234"). En la primera `session_start` el visor
(http://127.0.0.1:8765) se abre solo en tu navegador si no tenías ninguna pestaña abierta; si ya la tenías, salta
a la sesión nueva. Claude además te pasa el link.

## El visor

En el visor, lo más reciente queda arriba. La cabecera de la sesión queda fija y muestra cuántas llamadas se
hicieron a cada servicio (OpenSearch, CloudWatch, MySQL), cuántas fallaron o se rechazaron y lo gastado en
CloudWatch. Cada tarjeta lleva el ícono del servicio, la tool y el propósito de la consulta (`purpose`, que Claude
completa siempre).

Cada respuesta de `os_*`, `db_*` y `cw_*` trae el `query_id` de la consulta. Claude lo cita en sus notas para
indicar qué consulta sostiene cada hallazgo, y en el visor esos IDs son enlaces a su tarjeta. Si la consulta
guardó o marcó conocimiento (por ejemplo un `db_describe` o un `os_mapping`), la respuesta trae también `knowledge`
con las entradas afectadas (ver [Conocimiento entre investigaciones](conocimiento.md)).

Las consultas que bloquean los guards (escrituras, índices de sistema, etc.) también quedan en el timeline como
"rechazadas", para que el registro del caso esté completo.

**Aprobaciones:** si un `cw_query` supera su tope de costo (por defecto $0.25, comparado contra el estimado ×1.5)
o un `db_select` tiene un plan pesado (full scan de una tabla grande), la consulta no corre sola: queda
**esperando aprobación** en el visor, con botones "Aprobar y correr" y "Rechazar" y una notificación de escritorio.
Si nadie responde en 3 minutos (`approval_timeout_s`), se rechaza. Los límites duros no se pueden aprobar: el
límite por consulta que imponga el proveedor de logs y el corte de 15 s de MySQL siguen aplicando.

**Búsqueda:** el campo de arriba a la izquierda busca en notas, consultas, resultados, errores y borradores de
todas las sesiones; cada resultado lleva directo a su tarjeta.

**Documentar el caso:** el visor tiene "Copiar Markdown" y "Descargar .md" en cada sesión, y los borradores de
comentario que deja Claude aparecen con un botón para copiarlos. Nada se publica en Jira automáticamente.

**Un solo visor para todo.** El visor, las sesiones de Claude y `casebook viewer` usan la misma base
(`~/.local/share/casebook/sessions.db`), así que no chocan: el primero que arranca sirve el puerto 8765 y
los demás lo reutilizan. Puedes abrir el visor a mano antes o después de Claude: si lo cierras, el servidor de
Claude toma el puerto en unos segundos, y si ya hay uno corriendo, `viewer` simplemente lo abre en el navegador.
`casebook stop` cierra el que abriste con `viewer`/`start`; el de Claude Code se cierra junto con su sesión.
`casebook stop all` detiene todos, también los de Claude Code.

## Vigilar el canal de soporte

El panel **Slack** de la barra lateral tiene un toggle que activa la vigilancia del canal (`slack.watch`; el canal
lo trae el pack, y los demás ajustes están en **Ajustes → Slack**). Mientras está encendida:

- **Vigilar no gasta tokens:** se escuchan las notificaciones que Slack de escritorio muestra en Linux (D-Bus).
  Cuando llega una del canal, se lee lo nuevo con `claude -p` y el modelo de `slack.poll_model` (Haiku), con el
  **conector de Slack de claude.ai**: conéctalo una vez con `/mcp` → claude.ai Slack. El paquete no guarda
  credenciales de Slack.
- Para que funcione, Slack de escritorio tiene que estar abierto, sin «No molestar», y el canal tiene que notificar
  **Todos los mensajes nuevos** (en el canal: nombre → Notificaciones). Para las repreguntas, sigue los hilos que se
  analizaron. Slack no notifica el canal que tienes abierto en pantalla: para cubrir eso (o Slack cerrado), activa
  la **revisión de respaldo** (`slack.backup_poll_minutes`, apagada por defecto), que lee el canal cada tantos
  minutos y sí gasta uso de tu suscripción en cada revisión.
- Cada pedido nuevo (no los avisos, agradecimientos ni mensajes de bots) se analiza con `slack.analysis_model`
  (Sonnet) y las skills de triage e investigación del pack (`[slack.triage]`): lee el hilo y las imágenes,
  investiga en las fuentes (hasta `max_queries` consultas acotadas) y busca en Jira si ya hay una card con el mismo
  síntoma; si configuras `slack.code_paths`, también lee el código local (solo lectura) y la documentación que
  indique el pack. Deja una sesión **Slack · autor · hora** con el resultado (los que define el pack: respuesta directa,
  escalar con una card, qué información falta…), su **evidencia** (cada hallazgo enlaza a su consulta), la
  **confianza**, lo **descartado** y una respuesta sugerida corta para pegar en el hilo, coherente con lo que
  preguntó la persona. Llega una notificación de escritorio.
- Los hilos analizados se siguen una semana: si alguien responde y el autor vuelve a preguntar, esa repregunta se
  analiza **en la misma sesión** que el mensaje principal: el análisis lee la conclusión anterior y dice si cambia.
- **Revisar respuestas del hilo** (botón en la cabecera de una sesión de Slack): lee las respuestas del hilo
  posteriores al último análisis de esa sesión y, si hay algo nuevo, las analiza juntas **en la misma sesión**, aunque
  la vigilancia esté apagada. Si solo agradecen o confirman, no gasta un análisis (ofrece «Analizar igual»). Las
  respuestas analizadas quedan registradas y el hilo pasa a seguimiento.
- **Adjuntos:** las imágenes y capturas del hilo se le pasan al análisis por ID y tiene que decir qué muestra cada
  una (se ven en la conclusión, en «Adjuntos revisados»). Si se saltó alguna, la conclusión lo advierte.
- **Cada mensaje se analiza una sola vez.** Un registro marca cada mensaje como en cola, analizando o analizado, y
  lo automático nunca repite uno que ya está ahí (tampoco si falló). Si el visor se cierra a mitad de un análisis,
  ese mensaje queda como interrumpido y no se retoma solo.
- **Analizar el último mensaje** (botón del panel) busca el último mensaje de una persona en el canal y lo analiza,
  aunque la vigilancia esté apagada. Si ese mensaje ya está analizado o en curso, no lo repite: te lleva a esa
  sesión. Sí reintenta uno que falló o que quedó sin analizar por el tope.
- **Errores:** el panel muestra un título corto y qué hacer («El conector de Slack no está disponible» → `/mcp` →
  claude.ai Slack → Authenticate; «No encontré el canal» → revisa el ID en Ajustes), con el detalle técnico en «Ver
  detalle» y un contador si se repite. «Reintentar» vuelve a leer el canal. Si el conector no está autorizado, no se
  vuelve a llamar a Claude hasta que `claude mcp get` diga que está conectado (esa revisión no gasta tokens).
- **Nunca escribe en Slack:** cada llamada lleva `--disallowedTools` con las tools de escritura del conector y el
  repo las bloquea también en `.claude/settings.json`.
- Por defecto vigila únicamente en tiempo real a partir del momento en que se activa (no analiza el historial anterior). Al cerrar el programa (o si se cerró mal) la vigilancia queda apagada: los mensajes de mientras estuvo cerrado no se analizan, salvo que al encenderla de nuevo elijas una ventana (por ejemplo, la última hora). Al encender el toggle en el visor, puedes elegir la ventana de tiempo si deseas capturar mensajes previos recientes (por ejemplo, última hora). `slack.max_analyses_per_day` (50) pone un
  tope; pasado el tope, los pedidos quedan registrados sin analizar.
- Lo corre el proceso que sirve el visor, así que necesita el visor abierto (`casebook start` o una sesión
  de Claude Code).

## Retomar una investigación

Puedes decirle a Claude: **«Retoma la sesión 12»** o **«Continúa la investigación de SUP-1234»**.
La skill busca la sesión, la activa con `session_resume` y recupera la evidencia con `session_read`.
Funciona en el mismo chat, en uno nuevo y después de reiniciar el MCP, siempre que usen la misma base local.
Las consultas y notas nuevas se agregan al mismo ID. El visor también sigue la sesión reanudada.

`session_start` siempre crea otra sesión, aunque repitas nombre o card. Para consultar antecedentes mientras
trabajas en una sesión nueva, usa `session_list`/`session_search` y `session_read`: leer no cambia la sesión activa.
La selección activa pertenece a cada proceso MCP; dos chats con procesos distintos pueden trabajar en sesiones distintas.
Tras un reinicio o una reconexión del MCP debes reanudar explícitamente; no se selecciona automáticamente la
última sesión. Mientras no haya sesión activa, las consultas y notas no se ejecutan ni se registran: responden
«no hay sesión activa» con la última sesión con actividad como pista, para reanudarla con `session_resume`.

Se guardan consultas, resultados, notas y borradores, no la conversación completa de Claude. Antes de pausar,
pídele que deje una nota con hallazgos, evidencia y pendientes. Al reanudar recibe las últimas diez notas;
`session_read` permite leer el historial por páginas (`next_after`) y las tablas por filas (`row_offset`,
`max_rows`, `next_row_offset`). El JSON raw adicional sigue disponible en el visor. Leer el historial no vuelve
a ejecutar consultas ni garantiza que esos datos sigan vigentes.

## Comandos

| Comando | Qué hace |
|---|---|
| `casebook doctor` | **Requisitos** (ssh, los hosts de tus fuentes, que el servidor SSH responda, credenciales de la base, alias del pack, keyring) y **conexiones** (repara túneles, verifica sesiones). `--requisitos` para solo la primera parte |
| `casebook viewer` (o `start`) | Abre el visor para ver las sesiones, viejas o en vivo, sin necesidad de Claude Code. Si ya hay uno corriendo, abre ese |
| `casebook stop` | Detiene el visor abierto con `viewer`/`start` (igual que Ctrl-C en su terminal). Si el visor lo sirve Claude Code no lo toca: ese se cierra con su sesión |
| `casebook stop all` | Detiene todos los visores y servidores MCP de tu usuario, vengan de `start` o de Claude Code. Las sesiones de Claude Code abiertas pierden las herramientas de soporte hasta reconectarlas con `/mcp` o reiniciar |
| `casebook export [sesión] [-o archivo\|-] [--rows N]` | Exporta una sesión (id, nombre o card; por defecto la última) a Markdown |
| `casebook login <sistema>` / `logout <sistema>` | Guarda o borra las credenciales de `os`, `os-conversions`, `cw` o `db` |
| `casebook demo` | Escribe una sesión de ejemplo, para probar el visor |

Siguiente: [Conocimiento entre investigaciones](conocimiento.md)

Las sesiones abiertas con `session_start` sin nombre se llaman **Sin identificar · ID**. En cuanto el agente confirme la card,
usa `session_update(card="SUP-1240")`: cambia el nombre y la card de la sesión activa sin perder consultas.
Para ajustar solo el nombre, usa `session_update(name="SUP-1240 · Exportación CSV")`. Cada cambio queda
en el historial y se refleja en el visor en vivo. Para continuar una investigación, usa `session_resume`;
una investigación separada puede usar la misma card y un nombre corto que indique el tema.
