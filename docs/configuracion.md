# Configuración

La configuración tiene tres capas, y cada una pisa a la anterior clave por clave:

1. **Valores de fábrica**: neutros, sin ninguna fuente (OpenSearch, CloudWatch ni base).
2. **El pack activo**: la configuración del equipo (fuentes, canal de Slack, alias y textos del doctor). Se instala
   con `casebook pack install <carpeta>` y queda en `~/.local/share/casebook/packs/<nombre>`;
   `casebook pack show` dice cuál está activo, y `./install.sh` lo actualiza desde donde vino. Tiene el mismo
   formato que `config.toml`, más una tabla `[pack]` con `name`, `version` y `display_name` ([Packs](packs.md)).
3. **Tu `config.toml`**: `~/.config/casebook/config.toml`.

Sin pack ni fuentes en `config.toml`, el MCP solo trae las tools de sesiones y conocimiento, y el doctor avisa qué
falta. Una fuente se agrega sin editar el archivo con `casebook source add` (pregunta los campos del
proveedor) o en el visor, **Ajustes → Fuentes**; `source list`, `source test <id>` y `source remove <id>` completan.
Ver [Packs](packs.md) para instalar y armar packs. Una fuente nueva aparece al reiniciar Claude Code; los cambios de valores, en la próxima consulta.

Lo más común se cambia desde **Ajustes** en el visor
(`#/settings`): escribe este archivo, deja una copia en `config.toml.bak` y cada servidor MCP lo toma en la
próxima consulta, sin reiniciar. Host y puerto del visor sí piden reiniciar Claude Code. Desde el visor las
guardas tienen límites duros (por ejemplo, el tope de CloudWatch va de $0.01 a $5); a mano se puede poner
cualquier valor. Al guardar desde el visor el archivo se regenera y se pierden los comentarios; un valor igual al
del pack se borra del archivo, así los cambios del pack te llegan.

```toml
[viewer]
port = 8765
approval_timeout_s = 180   # cuánto espera una consulta cara a que la apruebes
auto_open = true           # abrir el visor en la primera session_start si no hay pestañas abiertas

[jira]
base_url = "https://<sitio>.atlassian.net/browse/"   # muestra "Abrir card" en el visor

[cloudwatch]
max_cost_usd = 0.25        # max_cost_usd por defecto de cw_query (sobre eso pide aprobación)
max_window_days = 7

[opensearch]
max_size = 10000           # size máximo de os_search

[sources.opensearch.prod]   # pisa una clave de una fuente del pack: aquí, el host de ssh
ssh_host = "mi_tunel_os_prod"

[sources.opensearch.staging]  # otro entorno: aparece en las tools (env="staging"), el doctor, el login y la extensión
provider = "dashboards-proxy"
domain = "vpc-….es.amazonaws.com"
tunnel_port = 9200         # ssh_host por defecto: os_tunnel_staging
system = "os-staging"      # opcional: el id de `casebook login <id>` (por defecto os-<entorno>)
label = "OpenSearch staging"            # opcional: el nombre en el instalador, el visor y la extensión
description = "índices de prueba"       # opcional: se le muestra a Claude en la descripción de las tools

[slack]
watch = false              # el toggle del panel Slack del visor
channel_id = "C…"          # el canal que se vigila (normalmente lo trae el pack)
channel_name = "soporte"   # como aparece en las notificaciones de Slack
backup_poll_minutes = 0    # revisión de respaldo sin notificación (0 = apagada; gasta tokens)
default_lookback_minutes = 0  # minutos de mensajes pasados a revisar al encender (0 = solo tiempo real)
ask_lookback_on_enable = true # preguntar ventana de tiempo al encender el toggle en el visor
poll_model = "haiku"       # modelo que trae los mensajes nuevos
analysis_model = "sonnet"  # modelo del triage de cada pedido
max_analyses_per_day = 50
code_paths = "/home/yo/repos/backend"  # carpetas con el código local, separadas por coma: el análisis las lee (solo lectura)

[doctor]                      # normalmente los trae el pack
shell_aliases = ["db-prod"]   # alias de shell que el equipo espera tener (aviso si faltan)
network_hint = "revisa la VPN" # qué decir cuando no se llega al servidor SSH

[db]                          # las guardas de MySQL
max_execution_ms = 15000
full_scan_max_rows = 200000   # EXPLAIN: full scan permitido hasta estas filas estimadas
plan_max_rows = 5000000       # EXPLAIN: filas estimadas máximas del plan
default_limit = 100           # LIMIT que se agrega a un SELECT sin LIMIT
max_limit = 5000              # LIMIT máximo aceptado

[sources.mysql]
provider = "mysql-ssh"
ssh_host = "bastion"          # el host de ~/.ssh/config por el que se abre el túnel
host = "replica.db.internal"  # la réplica de solo lectura, nunca el writer
database = "app"
reader_host_pattern = ""      # opcional: texto que tiene que estar en el host (en Aurora, "cluster-ro")

[sources.mysql.credentials]   # de dónde salen usuario y contraseña (si no hay, se usa `casebook login db`)
kind = "env-file"             # "env-file" (líneas `export X=...`), "env" (variables de entorno) o "keyring"
path = "~/.db/credentials"    # env-file
user_var = "DB_USER"
password_var = "DB_PASSWORD"
hint = "pídelas a tu equipo"  # lo que se le dice al usuario si faltan
fix = ""                      # un comando que las genera, si lo hay
```

El formato anterior (`[opensearch.envs.<env>]`, `provider` dentro de `[cloudwatch]`, y `ssh_host`/`host`/`credentials`
dentro de `[db]`) se sigue leyendo; si están los dos, manda `[sources.*]`. Si cambias el `provider` de una fuente del pack, la fuente se
reemplaza entera (las opciones del proveedor anterior no se arrastran).

## Textos para Claude

El pack también define cómo se le habla a Claude y qué se muestra en el visor. Sin pack, todo es genérico.

```toml
[texts]
domain = "casos de soporte de Acme"   # «Herramientas de solo lectura para investigar …»

[card]
prefix = "SUP"                         # el filtro de cards del visor; vacío = cualquier card
examples = ["SUP-1200", "SUP-1210"]    # ejemplos en las instrucciones, las tools y las ayudas

[sources.opensearch.prod]
hint = "familias: logs-*, users-*"     # qué hay adentro: va en la descripción de las tools

[sources.cloudwatch]
hint = "por ejemplo /ecs/api, /ecs/worker"

[sources.mysql]
hint = "users, orders, payments"
environment = "prod"                   # los esquemas quedan en el conocimiento como prod/<database>

[slack.triage]                         # el triage automático del canal
skill = "acme-triage"                  # la skill que lo guía (vacío = sin skill)
escalation = "ticket"                  # el resultado que pide una card (el análisis trae su título)
escalation_label = "Ticket"
reply_style = "en español, lista para pegar en el hilo"  # el análisis ya la pide corta (máx. 4 frases); esto fija idioma y tono
investigation_skill = "acme-investigation"  # opcional: la skill de investigación (método y recetas) que también carga
max_queries = 8                        # tope de consultas a las fuentes por análisis
allowed_tools = ["mcp__claude_ai_Acme__search_docs"]  # tools extra de solo lectura (documentación); nombres exactos

[slack.triage.outcomes]                # los resultados posibles y cómo se muestran
explain = "Se responde directo"
ticket = "Abrir ticket"
need_info = "Falta información"
not_issue = "No es un issue"
```

### Código local en el análisis de Slack

`slack.code_paths` (en tu `config.toml` o en Ajustes → Slack; es de cada máquina, no va en el pack) le da al
análisis automático acceso de **solo lectura** a esas carpetas para entender cómo funciona algo o confirmar un posible
bug. Tiene que ser una ruta absoluta de una carpeta que exista; no se aceptan la raíz, tu carpeta personal ni
carpetas que incluyan credenciales (casebook, `~/.ssh`, `~/.aws`…). Si editas `config.toml` a mano con una ruta
que no cumple, se ignora (sin dar acceso) y la app arranca igual; Ajustes y el asistente sí te dicen el motivo. Sin
valor, el análisis no lee archivos. Cuándo usar
el código, y cuándo la documentación, lo dice la skill del pack. Detalle de las protecciones en
[Seguridad](seguridad.md).

## Proveedores

Cada fuente (un entorno de OpenSearch, CloudWatch, la base) elige con `provider` el código que habla con ella. El
esquema de opciones de cada proveedor se valida al cargar el archivo: una opción desconocida o un obligatorio que
falta es un error que nombra la fuente. Si el proveedor no está instalado (un plugin que falta), la fuente queda
deshabilitada y el doctor lo dice.

| Fuente | `provider` | Sesión | Opciones |
|---|---|---|---|
| `[sources.opensearch.<env>]` | `dashboards-proxy` (por defecto) | navegador (cookie de Dashboards) | `domain`, `tunnel_port`, `ssh_host` |
| `[sources.opensearch.<env>]` | `direct` | keyring (`login os-<env>`) | `url`, `auth` (`basic`, `api-key` o `none`), `ssh_host` (túnel opcional), `verify_tls` |
| `[sources.cloudwatch]` | `aws` | perfil de AWS (`aws sso login`) | `profile`, `region`, `price_per_gb`, `log_group_prefixes` |
| `[sources.mysql]` | `mysql-ssh` (por defecto) | `[sources.mysql.credentials]` o keyring | `ssh_host`, `host` (reader), `port`, `database`, `reader_host_pattern` |

```toml
[sources.opensearch.lab]       # un cluster con la API REST abierta, sin Dashboards
provider = "direct"
url = "https://search.lab.internal:9200"
auth = "basic"                 # el usuario y la contraseña se guardan con `casebook login os-lab`
ssh_host = "bastion-lab"       # opcional: `ssh -L` hacia la URL

[sources.cloudwatch]           # Logs Insights directo con tu perfil de AWS (pip install 'casebook[aws]')
provider = "aws"
profile = "soporte"
region = "us-east-1"
log_group_prefixes = ["/ecs/"] # opcional: solo estos log groups
```

Con `aws`, el estimado de `cw_estimate` y `cw_query` es aproximado: CloudWatch no estima consultas, así que se toma
lo almacenado del log group y se supone que se reparte parejo en el tiempo que cubre (su retención o su edad).

Un plugin suma proveedores con un entry point del grupo `casebook.providers` que apunta a la clase (con
`kind`, `name`, `auth`, `fields` y los métodos de `providers/base.py`).
