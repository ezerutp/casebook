# Conocimiento entre investigaciones

El MCP mantiene `knowledge.db` **junto a `sessions.db`**, en un archivo SQLite independiente. No requiere un
servicio nuevo. Guarda automáticamente los metadatos exitosos de `db_tables`, `db_describe` y `os_mapping`;
no copia las filas de consultas de clientes. Los catálogos conservan el patrón consultado y los mappings
corresponden solo al índice concreto observado. Cada entrada tiene fuente, entorno, entidad, tipo, temas,
fecha, evidencia e historial de revisiones.

## Cómo lo usa el agente

Antes de redescubrir una entidad, el agente busca con `knowledge_search` y lee las entradas relevantes con
`knowledge_read`. La búsqueda es textual por palabras (AND), con filtros exactos y paginación; no necesita
embeddings ni cargar toda la base. `knowledge_record` agrega relaciones, ejemplos parametrizados y referencias
al código con evidencia. `knowledge_invalidate` marca una entrada desactualizada sin borrar su historia.
Las referencias a consultas deben existir y haber terminado correctamente; las de código incluyen archivo y
revisión, pero no se verifican automáticamente contra el repositorio.

## Estados

**verified** para un esquema observado correctamente en esa fecha; **pending** para afirmaciones o
correcciones manuales; **stale** cuando hay una contradicción de esquema. Un esquema verificado puede cambiar:
el agente comprueba alcance, entorno y fecha antes de reutilizarlo. Los fallos explícitos de tabla/columna
marcan los esquemas afectados para revisión; problemas de conexión, permisos o timeout no lo hacen. Si no
había esquema de una tabla inexistente, se guarda una observación pendiente, no una regla permanente.
Los errores de OpenSearch requieren revisión explícita: un HTTP 400 por sí solo no invalida un mapping.

## En el visor

Abre **Conocimiento** (`#/knowledge`) para buscar, revisar evidencia, ver versiones anteriores,
corregir, marcar entradas desactualizadas o **eliminarlas** con confirmación. Eliminar borra la entrada y sus
revisiones, conserva las sesiones y no reutiliza su ID. Reimportar las mismas observaciones no la restaura;
una nueva consulta de esquema puede crear una entrada nueva. Las tarjetas de consultas muestran enlaces al conocimiento que
actualizaron. **Importar esquemas de sesiones** recorre el historial local sin consultar servicios remotos ni
modificar sesiones: recupera metadatos y errores de esquema, no interpreta notas ni importa datos de clientes.
Es repetible y no duplica observaciones ni sobreescribe correcciones con evidencia más antigua.

## Concurrencia y respaldo

Las ediciones requieren la revisión actual de la entrada; si otro chat la cambió, se pide releer para no
pisarlo. Las nuevas observaciones pueden reemplazar el esquema capturado: guarda explicaciones como entradas
separadas. Se usan WAL y transacciones cortas. Si falla la memoria, la consulta continúa y muestra un aviso.
La evidencia incluye un resumen independiente: borrar una sesión puede romper su enlace, pero no borra el
conocimiento. Para respaldar una base activa usa la API de backup de SQLite; para copiar el archivo directamente,
cierra primero todos los procesos MCP y visores para no omitir cambios pendientes en el WAL.

## Compartir conocimiento

Cada entrada tiene una **firma**: un hash corto de su fuente, entorno, entidad, tipo, título, contenido y temas.
El estado, las fechas y la evidencia quedan fuera, así la misma entrada firma igual en cualquier máquina. Se
calcula al guardar; las entradas anteriores a las firmas se firman al exportar.

```bash
casebook knowledge export                 # todas las entradas a casebook-conocimiento-AAAA-MM-DD.json
casebook knowledge import archivo.json    # solo entran las que no tienes
```

En el visor son los botones **Exportar** e **Importar archivo** de Conocimiento. Siempre se exporta todo; al
importar:

- Una firma que ya tienes, o que ya importaste alguna vez, se omite. Por eso reimportar un archivo con 33
  entradas cuando ya tenías 30 agrega 3, y una entrada importada que eliminaste no vuelve.
- Si la misma entidad existe con otro contenido gana la más reciente, como una revisión nueva: la anterior queda
  en el historial. Lo local más reciente no se pisa.
- La entrada conserva su estado (`verified`, `pending` o `stale`) y su evidencia dice de qué archivo vino. Los
  enlaces a sesiones y consultas no viajan: son del historial de quien exportó.
- La firma se recalcula con el contenido: no se confía en la que trae el archivo. El contenido pasa por la misma
  validación y redacción que cualquier entrada. Importa archivos de gente de tu equipo: una entrada `verified`
  ajena la reutiliza el agente como si la hubieras observado tú.

### La rama del equipo

Un pack puede declarar una rama git donde el equipo comparte su conocimiento ([Packs](packs.md)):

```bash
casebook knowledge pull    # trae la rama e importa lo que falta
casebook knowledge push    # une lo tuyo con la rama y lo sube (pide confirmación)
```

`pull` también corre al final de `pack install` y `pack update`, y es el botón **Traer del equipo** del visor;
si falla (red, acceso), el pack queda instalado y se avisa. `push` sube **todo** tu conocimiento local a
`knowledge.json` de la rama: lo ve cualquiera con acceso al repo. Si otra persona subió antes, vuelve a unir y
reintenta; nadie resuelve conflictos de git a mano. La primera vez crea la rama, sin historia en común con el pack.
`--repo` y `--branch` usan otro destino que el del pack.

## Distribución

Las skills de un pack pueden incluir este flujo. Para distribuir sus cambios a las instalaciones existentes,
publica una versión nueva del pack (`casebook pack update` o `pack install`) y reconecta el MCP.
