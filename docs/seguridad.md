# Seguridad

- **Solo lectura:** cada tool arma su propia ruta a partir de operaciones conocidas (`_search`, `_count`,
  `_cat/indices`, `_mapping`; en CloudWatch, consultas de Logs Insights). Se rechazan los índices de sistema y los
  patrones `*` a secas.
- **MySQL:** si la fuente define `reader_host_pattern` (en Aurora, `cluster-ro`), solo se conecta a un host que lo
  contenga, y siempre verifica `@@innodb_read_only = 1` o `@@super_read_only = 1` antes de cada sesión. Cada consulta corre dentro de `START TRANSACTION READ ONLY` y la sesión tiene `MAX_EXECUTION_TIME` de 15 s. Además se acepta una sola
  sentencia SELECT/SHOW/DESCRIBE/EXPLAIN validada con sqlglot (sin `INTO`, `FOR UPDATE`, `SLEEP`, `EXPLAIN
  ANALYZE`, ...), con LIMIT forzado (máximo 5000) y un EXPLAIN previo que rechaza los full scans de tablas grandes.
- **Costo de CloudWatch:** se estima antes de correr; por encima del tope necesita tu aprobación en el visor. Un
  proveedor con límites propios (por consulta) los aplica además.
- **Visor:** escucha solo en 127.0.0.1. Aprobar y cancelar exigen un header propio y validan el `Origin`, así que
  otra página abierta en el navegador no puede disparar esas acciones.
- **Redacción:** tokens, credenciales y teléfonos se enmascaran antes de guardar resultados.
  Los emails completos quedan disponibles para el agente y en el historial local (incluidos API y exportaciones
  Markdown). El visor los oculta por defecto en cada consulta; «Mostrar correos» / «Ocultar correos» cambia
  solo su presentación en esa tarjeta, tanto en tablas como JSON. Copiar CSV respeta esa visibilidad.
  No es cifrado ni control de acceso. Las consultas antiguas ya enmascaradas no permiten recuperar originales.
- **Credenciales:** Claude nunca ve cookies ni contraseñas.
- **Extensión de Chrome (opcional):** no trae dominios fijos: solo puede leer cookies de los dominios de tus fuentes,
  y solo después de que le das permiso desde su ventanita (Chrome exige permiso sobre el dominio de la cookie).
  Entrega solo las que Chrome
  mandaría a la URL de validación, igual que el login con ventana aparte, y solo las entrega al native host del paquete; Chrome no deja que otra extensión hable
  con ese host (`allowed_origins`), y el host rechaza a quien no sea esta extensión. No guarda ni registra
  valores: el host valida cada cookie contra la API antes de guardarla en el keyring, y su archivo de estado
  (`chrome-extension.json`) tiene solo resultados y horas. La extensión lee tu perfil habitual de Chrome, a
  diferencia de la ventana aparte de Playwright.
- **Slack (opcional):** la vigilancia del canal de soporte escucha en el bus de sesión las notificaciones de
  escritorio (como `dbus-monitor`): solo mira de qué app y canal son, no guarda su texto. Lee el canal con el
  conector de Slack de claude.ai a través de `claude -p`; el paquete no ve ni guarda credenciales de Slack. Solo se permiten las tools
  de lectura del conector (`--allowedTools`) y cada llamada niega explícitamente las de escritura
  (`--disallowedTools`: enviar o programar mensajes, reacciones, canvas, listas, subir archivos). El análisis también
  puede buscar y leer cards con el conector de Jira de claude.ai (solo `searchJiraIssuesUsingJql`, `getJiraIssue` y
  `getAccessibleAtlassianResources`); crear, editar, comentar o mover cards y escribir en Confluence queda negado.
  Con `slack.code_paths` configurado, el análisis también lee el código local, solo lectura: una regla
  `Read(//ruta/**)` por carpeta limita `Read`, `Grep` y `Glob` a esa carpeta y el resto del disco sigue negado; dentro
  de ella se niegan los archivos que suelen guardar credenciales (`.env`, `*.pem`, `*.key`, `*.tfvars`, `id_rsa*`,
  `.git/config`, `application-dev*.properties`, `*credentials*.sh|json|yml…`, `*secrets*.*`…). `Bash`, `Write` y `Edit` siguen negados, así que no ejecuta ni modifica nada, y no se aceptan
  carpetas que incluyan los datos de casebook, `~/.ssh` o `~/.aws`. El pack puede sumar tools de documentación con
  `[slack.triage] allowed_tools`: solo nombres exactos de tools de lectura (se rechazan las que por su nombre
  escriben, como `create…`, `delete…` o `refund…`), nada de lo que está negado, y `pack install` muestra cuáles trae. `claude -p` corre
  fuera del repo
  (`~/.local/share/casebook/slack-watch/`). El contenido del canal se trata como datos, no como
  instrucciones. El estado (`slack-watch.json`) guarda cursores, autores y resultados, no el texto de los mensajes;
  el texto de cada pedido queda en la sesión del visor, igual que cualquier consulta.
