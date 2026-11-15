# Consultas manuales desde el visor

> **Estado:** implementada en 0.8.0 (las tres fases). El uso está en [Uso del visor](../uso.md#consultas-tuyas-desde-el-visor).
> Este documento queda como el diseño; abajo, lo que cambió al implementarla.

## Cambios respecto de la propuesta

- **Ejecuciones:** en vez de pestañas dentro de una sola tarjeta, cada ejecución es su propia tarjeta en el
  timeline y la original y sus repeticiones muestran una fila de chips «Ejecuciones» que lleva a cada una. Así el
  timeline sigue cronológico y cada ejecución conserva sus tablas, JSON y aprobaciones.
- **Filtro:** un solo chip «Tuyas» junto a los demás (manuales y fijadas, con las originales atenuadas), en vez de
  Todo · Agente · Manuales.
- **Ejecución en el visor:** no hizo falta separar la lógica de cada tool. `manual.py` registra las mismas tools en
  un servidor MCP propio con un Recorder que escribe en la sesión elegida (`ManualRecorder`); los guards, la
  redacción y la captura de conocimiento son literalmente las mismas.
- **Rechazos:** un guard que bloquea una consulta tuya se muestra en el editor; no deja tarjeta «rechazada».
- **Argumentos:** `query.started` guarda ahora `args` (los argumentos de la tool) para poder repetir; en eventos
  anteriores se reconstruyen de `params` y del texto de la consulta.
- **Guía en vivo:** «Traer campos en vivo» y «Listar» corren `os_mapping`/`db_describe` y
  `os_indices`/`db_tables`/`cw_log_groups` como consultas tuyas de la sesión.

## Problema

Cuando el agente termina, el visor solo sirve para leer. Para comprobar si algo cambió, probar una variante o
lanzar una consulta que al agente no se le ocurrió, el ingeniero tiene que pedírsela a Claude. Y lo que hace
por fuera de casebook (en Dashboards, un cliente MySQL o la consola de AWS) el agente no lo ve, no queda
redactado ni pasa por los guards.

## Principio rector

**Una consulta manual es un evento más de la sesión, no un mundo aparte.** Se guarda en la misma tabla
`events`, con el mismo ciclo `query.started` → `query.finished` / `query.failed`, los mismos guards
(`check_sql`, `check_search_body`, `check_index`, la ventana máxima de `timerange`) y la misma redacción.
Solo agrega campos al payload de `query.started`:

| Campo | Valor | Para qué |
|---|---|---|
| `origin` | `"agent"` (por omisión, ausente en eventos viejos) o `"manual"` | Distinguirla en el visor, `session_read` y el export |
| `parent_query_id` | `query_id` de la consulta de origen | Agrupar repeticiones y derivadas en la card original |
| `run_mode` | `"repeat"`, `"edit"` o `"new"` | Cómo se creó |

Con eso, `session_read`, el export a Markdown, la búsqueda y el aprendizaje de conocimiento
(`knowledge.observe`) la tratan sin cambios.

## 1. Repetir desde la card

Cada card de consulta terminada (o fallida) suma dos acciones: **↻ Repetir** y **✎ Editar y ejecutar**.

### Historial de ejecuciones

Las ejecuciones con el mismo `parent_query_id` se agrupan en la card original:

```
Buscar conversiones del cliente en la ventana del reporte
  Ejecución 1 · agente · 14:02 · 37 filas
  Ejecución 2 · tú     · 16:40 · 41 filas (+4)
  Ejecución 3 · tú     · 17:15 · 41 filas (=)
```

- Por defecto se muestra la última ejecución; las anteriores se abren con pestañas o un desplegable.
- **Comparación:** siempre el delta de conteo. En resultados tabulares, además, un diff de filas
  (nuevas, desaparecidas, cambiadas) usando como clave `_id` en OpenSearch o la PK en MySQL cuando
  esté entre las columnas. Sin clave, solo el delta de conteo.

### Ventana de tiempo

`cw_query` ya guarda `start` y `end` resueltos a ISO en `params`. Al repetir se ofrece:

- **Misma ventana** (por omisión): reproduce exactamente lo que vio el agente.
- **Desplazar a ahora:** misma duración, terminando en `now`; responde "¿sigue pasando?".

Las consultas de OpenSearch o MySQL con fechas literales en el cuerpo se repiten tal cual; para cambiarlas se
usa *Editar y ejecutar*.

### Qué no se repite

Las tools de descubrimiento (`os_mapping`, `db_describe`, `db_tables`, `os_indices`, `cw_log_groups`,
`cw_estimate`) no muestran *Repetir*: no aportan evidencia del caso y el conocimiento ya las cubre.

## 2. Editar y ejecutar

Abre el editor (sección 3) precargado con fuente, entorno, esquema y consulta de la card. Al ejecutar se crea
una card nueva marcada "derivada de #abc123" (`run_mode: "edit"`). La card original no se modifica: la
evidencia del agente queda intacta.

## 3. Consulta nueva con guía

Botón **+ Consulta** en la vista de la sesión. Abre un panel por pasos donde cada paso alimenta la guía del
siguiente:

| Paso | Qué ve el ingeniero | De dónde sale la guía |
|---|---|---|
| **Fuente** | OpenSearch, MySQL, CloudWatch | `sources.py`: solo las configuradas, con su estado de conexión (health) |
| **Entorno** | prod, prod-conversions, qa, qa-conversions… | `config.env_names`; prod resaltado en rojo, qa neutro |
| **Esquema** | índice, tabla o log group con autocompletado | Primero entradas `verified` de `knowledge.db`; si no hay, `os_indices` / `db_tables` / `cw_log_groups` en vivo |
| **Guía** | Campos con tipo, índices de la tabla, plantillas | `os_mapping` / `db_describe` (o su entrada verified) y plantillas por fuente ("lead por email", "conversiones de un cliente en una ventana", "errores de una integración") |
| **Escritura** | Editor con autocompletado de campos y resaltado | Lo anterior más **consultas parecidas que ya funcionaron** en otras sesiones (`query.finished` sobre el mismo índice, tabla o log group) |
| **Antes de ejecutar** | Validación y costo en vivo | Guards en seco mientras se escribe; `cw_estimate` en CloudWatch; `os_count` como "previsualizar cantidad" en OpenSearch |

El campo **purpose** es obligatorio también aquí: es el título de la card y lo primero que lee el agente.

Las plantillas son conocimiento técnico, no datos de clientes: se guardan como ejemplos parametrizados en
`knowledge.db` (ya admitidos por `knowledge_record`) y se pueden compartir con el resto del conocimiento.

## 4. Que el agente no las ignore

Tres niveles, de menor a mayor esfuerzo:

1. **Pasivo** (sale gratis del principio rector): aparecen en `session_read` con `origin: "manual"`.
2. **Activo con la sesión viva:** si el agente sigue trabajando, el decorador `expected_errors`
   (`tools/common.py`) agrega a la respuesta de su siguiente tool un bloque `engineer_activity` con las
   consultas manuales nuevas desde la última que se le informó (purpose, fuente, entorno, conteo y delta, y
   el `query_id` para leerla con `session_read`). No depende de que el agente decida releer.
3. **Con la sesión cerrada:** `session_resume` informa "N consultas manuales posteriores a la conclusión", y
   `session_finish` y la revisión las exigen como evidencia a considerar. En el visor, una consulta manual
   en una sesión cerrada se marca como **evidencia nueva posterior a la conclusión**. No reabre la sesión
   sola, respetando la regla de no reabrir automáticamente; el ingeniero decide si pedir `session_reopen`.

Para el nivel 2 basta un cursor por proceso MCP (último `event id` manual informado), en memoria igual que
`Recorder.session_id`.

## 5. Ver arriba las consultas manuales

El timeline sigue siendo cronológico: el orden muestra cómo razonó el agente y en qué momento llegó cada
evidencia, y mover las cards lo rompería. Para tener a la vista lo que hizo el ingeniero se agregan tres
piezas encima del timeline:

### Franja "Tus consultas"

Una franja fija arriba de la sesión, visible solo si hay consultas manuales. Cada una es una card compacta:

```
Tus consultas (3)                                          [Solo manuales ▾]
 ↻ Conversiones del cliente   ·  prod · OS · 41 filas (+4)  · 16:40   → ir
 ✎ Mismo filtro sin tag       ·  prod · OS · 12 filas       · 16:52   → ir
 + Errores de la integración  ·  prod · CW · 3 filas · $0.002 · 17:05  → ir
```

- El icono indica el tipo: ↻ repetida, ✎ editada, + nueva.
- **→ ir** hace scroll a la card completa en su lugar del timeline y la resalta un momento.
- La franja se colapsa y recuerda el estado por sesión (`localStorage`).

### Filtro del timeline

Chips sobre el timeline: **Todo · Agente · Manuales**. "Manuales" muestra también la card original de cada
repetición, en gris, para tener contexto de la comparación.

### Fijar arriba

Cualquier card, manual o del agente, tiene **📌 Fijar**. Las fijadas encabezan la franja. Sirve para dejar
a la vista la evidencia clave del caso. Se guarda como evento `query.pinned` / `query.unpinned` (no en
`localStorage`): así el agente sabe qué consideró importante el ingeniero y el export lo destaca.

### Alternativa descartada

Un orden "manuales primero" que reordene el timeline completo. Mezcla momentos distintos de la
investigación y hace confusa la comparación con la ejecución original del agente.

## Detalles técnicos

### Dónde se ejecuta

El visor es un proceso FastAPI separado del MCP, pero ya crea clientes de las fuentes
(`/api/sources/{id}/test`) y comparte `sessions.db`. Las consultas manuales se ejecutan en el visor con un
`Recorder` sobre el mismo `Store`.

El cambio previo necesario es **separar la lógica de cada tool de su registro en el MCP**: funciones como
`run_db_select(cfg, recorder, env, sql, limit)`, `run_os_search(...)`, `run_cw_query(...)` que el MCP y el
visor llaman por igual. Así los guards, la redacción, el estimado y el `finish` no se duplican. Es el refactor
más grande de la propuesta.

### Endpoints nuevos

| Método | Ruta | Para qué |
|---|---|---|
| `POST` | `/api/queries/{query_id}/repeat` | Repetir; body `{window: "same" \| "shift"}` |
| `GET` | `/api/queries/{query_id}/args` | Tool, argumentos y purpose de una consulta (para «Editar y ejecutar») |
| `POST` | `/api/sessions/{session_id}/queries` | Ejecutar una consulta editada o nueva; body `{tool, args, purpose, run_mode, parent_query_id?, wait?}` |
| `POST` | `/api/queries/{query_id}/pin` | Fijar o desfijar; body `{pinned}` |
| `GET` | `/api/manual/options` | Fuentes configuradas, entornos y límites |
| `POST` | `/api/manual/validate` | Guards en seco y estimado, sin ejecutar ni registrar; `count` cuenta en OpenSearch |
| `GET` | `/api/manual/guide` | Entidades, esquema, plantillas y parecidas (`?source=&env=&entity=`) |
| `POST` | `/api/sessions/{session_id}/discover` | Listar entidades o traer campos en vivo (`{source, env, what, entity}`) |

Todos con `require_viewer`, como el resto de los endpoints que escriben.

### Costo y aprobación

En CloudWatch el estimado se muestra antes de ejecutar y por encima de `max_cost_usd` se pide una
confirmación explícita en el propio panel. No se usa `query.approval_requested`: quien aprueba ya es el
ingeniero. Los límites duros (ventana máxima, `DB_MAX_LIMIT`, `MAX_OS_SIZE`) aplican igual que al agente.

### Cancelación

Se reutiliza `query.cancel_requested` y `/api/queries/{query_id}/cancel`, que ya existen.

### Gate de revisión

`review.query_gate` controla al agente durante la revisión. Las consultas manuales **no** pasan por el gate:
el ingeniero puede consultar en cualquier momento.

### Concurrencia

MCP y visor escriben en el mismo SQLite. Las escrituras son eventos cortos, así que alcanza con WAL y
`busy_timeout`. Hay que verificar que `Store` ya los configure antes de habilitar escrituras desde el visor.

## Impacto en los packs (por ejemplo, casebook-hyros)

Nada cambia en un pack hasta que casebook publique la fase 1. Cuando salga:

- **Skill de investigación:** agregar cómo tratar las consultas manuales. Son evidencia del ingeniero: se
  citan por `query_id` como las demás, no se repiten si ya responden la pregunta, `engineer_activity` se lee
  antes de seguir y una card fijada es evidencia que el ingeniero considera clave.
- **Plantillas del editor guiado (fase 3):** salen como ejemplos parametrizados en la rama de conocimiento del
  pack (`[pack.knowledge]`), sin crear un mecanismo nuevo. Las recetas ya nombran los índices, tablas y tags
  de log de cada tipo de caso, así que son la fuente natural para escribirlas.
- **Proveedores de plugins:** el visor los usa igual que el MCP, así que no necesitan cambios de código. Hay
  que probar dos cosas: que la sesión del proveedor (cookie o keyring) esté disponible en el proceso del visor,
  y que los límites del backend (por ejemplo, el costo diario de CloudWatch en devops) cuentan también las
  consultas manuales.
- **`requires`:** subir la versión mínima de casebook en `pack.toml` a la que traiga la función.

## Fases

1. **Repetir + historial de ejecuciones + delta de conteo**, con `origin: "manual"` visible en
   `session_read`. Incluye el refactor de las tools. Valida la idea con poco código de UI.
   Suma la franja "Tus consultas" con **→ ir** y el filtro Todo / Agente / Manuales.
2. **Editar y ejecutar** con editor básico: validación en seco, estimado y diff de filas. Suma **Fijar**.
3. **Consulta nueva con guía completa**, plantillas, "consultas parecidas" y el aviso activo
   `engineer_activity`.

## Preguntas abiertas

- ¿El diff de filas se calcula en el visor (JavaScript, sobre los resultados ya guardados) o en el servidor?
  Los resultados ya están en los eventos, así que el visor alcanza salvo resultados muy grandes.
- ¿Se permite ejecutar manualmente en una sesión que no es la activa del MCP? Propuesta: sí; el aviso activo
  solo aplica si coincide con la sesión activa, y el resto llega por `session_resume`.
- ¿Las plantillas por fuente vienen del pack del equipo (`pack.toml` / `knowledge/`) además de `knowledge.db`?
