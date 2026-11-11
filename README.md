# casebook

Herramientas MCP de solo lectura para investigar casos de soporte con un agente (Claude Code, Codex u OpenCode):
consultas a OpenSearch, CloudWatch Logs Insights y MySQL con guardas de costo y de solo lectura, un visor local
donde se ve en vivo cada consulta que hace el agente, sesiones que se retoman y memoria técnica compartida entre
investigaciones.

casebook no sabe nada de tu empresa: las fuentes, el canal de Slack, los textos y las skills de un equipo viven en
un **pack**, que se instala aparte (una carpeta, un zip o un repo git). Sin pack, la app arranca sin fuentes y las
puedes agregar a mano.

| Tool | Qué hace |
|---|---|
| `session_finish`, `session_review_status`, `session_review_complete`, `session_reopen` | Cierre formal y loop de revisiones adicionales, con estado y progreso en el visor; reabre una sesión cerrada si se pide más trabajo |
| `session_start`, `session_update`, `session_note` | Agrupan las consultas de un caso y dejan conclusiones en el timeline |
| `session_list`, `session_search`, `session_read`, `session_resume` | Buscan y recuperan investigaciones guardadas; permiten continuar la misma sesión desde otro chat |
| `knowledge_search`, `knowledge_read`, `knowledge_record`, `knowledge_invalidate` | Memoria técnica local compartida entre investigaciones; búsqueda por fuente, entorno y tema |
| `jira_comment_draft`, `session_export` | Borrador de comentario para la card (no se publica) y export de la sesión a Markdown |
| `os_indices`, `os_mapping`, `os_count`, `os_search` | OpenSearch: directo (URL + usuario o API key) o vía la consola de Dashboards por túnel SSH |
| `cw_log_groups`, `cw_estimate`, `cw_query`, `cw_context` | CloudWatch Logs Insights con un perfil de AWS (o el proveedor de un plugin) |
| `db_tables`, `db_describe`, `db_select` | MySQL: la réplica de solo lectura, por túnel SSH, con EXPLAIN previo |

Las tools de cada fuente aparecen solo si esa fuente está configurada.

## Inicio rápido

```bash
git clone https://github.com/ezerutp/casebook.git ~/repos/casebook
cd ~/repos/casebook
./install.sh          # instala el paquete y abre el asistente (gráfico o en la terminal)
casebook doctor       # todo en ✓ significa que está listo
```

Después pídele al agente «investiga SUP-1234»: el visor (http://127.0.0.1:8765) se abre solo y muestra cada
consulta en vivo.

## Instalación

La instalación tiene dos partes:

1. **`install.sh` instala casebook:** busca Python 3.12+, crea el entorno, instala el paquete, deja el comando
   `casebook` en `~/.local/bin` (avisa si esa carpeta no está en tu `PATH`) y actualiza el pack activo si ya había
   uno. Desde ahí `casebook` funciona en cualquier carpeta, aunque no abras el asistente. No usa sudo y se puede
   correr de nuevo cuando quieras.
2. **El asistente lo configura** (`casebook setup`, gráfico o en la terminal):
   - instala el pack de tu equipo (o te deja seguir sin pack y agregar fuentes a mano);
   - registra el servidor MCP y las skills en los agentes que elijas (Claude Code, Codex, OpenCode);
   - revisa los requisitos y la extensión de Chrome;
   - activa la vigilancia de Slack (opcional) e inicia las sesiones de tus fuentes.

La primera vez, y al cambiar de modo, `install.sh` abre el asistente al terminar; si lo saltas (`--no-setup`) o
lo cierras a medias, lo vuelve a abrir la próxima vez hasta que llegues al final. Al reinstalar avisa que
casebook quedó instalado y pregunta si abrirlo: elige «Abrir el asistente» para sumar entornos o fuentes, cambiar
de agentes o de pack, o volver a cargar la extensión. Por defecto termina sin tocar tu configuración, y lo abres
cuando quieras con `casebook setup`.

**Modos** (el instalador pregunta; viene marcado el de tu última instalación):

| Modo | Dónde queda | Para qué |
|---|---|---|
| **Instalación local** (`--local`) | Una copia propia en `~/.local/share/casebook/venv` | Usarlo. El repo se puede mover o borrar; los cambios del repo no se notan hasta actualizar |
| **Modo desarrollo** (`--dev`) | Editable desde el `.venv` del repo | Trabajar en casebook: tus cambios aplican con solo reconectar el MCP |

**Opciones de `install.sh`:**

| Opción | Qué hace |
|---|---|
| `--local` / `--dev` | Elige el modo sin preguntar |
| `--gui` / `--consola` | Abre el asistente gráfico o el de terminal, sin preguntar |
| `--setup` | Abre el asistente al terminar, también al reinstalar |
| `--no-setup` | Solo instala el paquete; el asistente queda para después |
| `--agents=claude,codex,opencode` | En qué agentes registrar el MCP y las skills, sin preguntar |
| `--skip-mcp` | No registra el servidor MCP en los agentes |
| `--skip-skill` | No instala las skills en los agentes |
| `--no-login` | Se salta el paso de inicio de sesión |
| `--preview` | Muestra el asistente sin cambiar nada (necesita una instalación previa) |
| `--uninstall` | Detiene los procesos y quita la instalación, el comando, las skills y el registro del MCP. Pregunta si borrar las credenciales y deja tus sesiones, conocimiento, exports, logs y config |
| `--uninstall --force` | Borra todo sin preguntar, incluidas credenciales, bases, config y el `.venv` del repo |

`--gui`, `--consola`, `--agents`, `--skip-mcp`, `--skip-skill` y `--no-login` configuran el asistente, así que
también lo abren al reinstalar. Detalle de cada paso en [Instalación](docs/instalacion.md).

## Actualizar

> [!IMPORTANT]
> Con la instalación local, casebook corre desde su propia copia: lo que cambies o traigas al repo no se ve
> hasta actualizarla.

| Comando | Instala | Cuándo |
|---|---|---|
| **`casebook update`** | `main` del remoto: hace `git fetch` en el repo de donde instalaste y arma el paquete desde `origin/main` | Para traer la última versión. No toca tu rama ni tus cambios locales |
| **`casebook update-dev`** | Tu copia de trabajo tal como está: la rama actual y los cambios sin commit | Para probar tus cambios sin pasar a modo desarrollo |

Los dos vuelven a copiar la extensión de Chrome si está registrada, y no abren el asistente. Después reinicia Claude
Code (o `/mcp` → reconectar casebook) y recarga el visor. Tus sesiones, conocimiento y credenciales no se tocan.
En modo desarrollo no hacen falta: basta `git pull`.

## Comandos

| Comando | Qué hace |
|---|---|
| `casebook --version` / `casebook -v` | Muestra la versión instalada de casebook |
| `casebook setup [--gui \| --consola] [--preview]` | Abre el asistente de configuración |
| `casebook update` / `casebook update-dev` | Actualiza la instalación local (ver [Actualizar](#actualizar)) |
| `casebook doctor [--requisitos]` | Revisa requisitos y conexiones, y repara túneles. `--requisitos`: solo la máquina, sin probar sesiones |
| `casebook viewer [--no-browser]` (o `start`) | Abre el visor de sesiones, viejas o en vivo, sin necesidad del agente |
| `casebook stop [all]` | Detiene el visor abierto con `viewer`. `all`: todos los visores y servidores MCP de tu usuario |
| `casebook login <sistema> [--browser-timeout S] [--ventana-separada]` | Inicia sesión en un sistema (`os`, `cw`, `db`, `db-qa`…; ver `source list`) |
| `casebook logout <sistema>` | Borra las credenciales guardadas de un sistema |
| `casebook source list \| add \| test <id> \| remove <id>` | Fuentes en tu `config.toml`. `add [tipo] [--provider P] [--env E] [--set CLAVE=VALOR]` |
| `casebook pack show \| list \| install <origen> \| update \| remove <nombre>` | El pack del equipo: el activo, los instalados, instalar desde carpeta, zip o URL git, reinstalar, quitar. `-y` sin preguntar |
| `casebook pack init <carpeta> \| build <carpeta> \| export` | Arma un pack de ejemplo, el zip para compartir, o exporta tu configuración como pack (`--name`, `--version`, `-o`) |
| `casebook extension [install \| uninstall] [--no-abrir]` | Registra o quita el native host de la extensión de Chrome |
| `casebook export [sesión] [-o archivo\|-] [--rows N]` | Exporta una sesión a Markdown (por defecto la más reciente) |
| `casebook demo [--fast]` | Escribe una sesión de ejemplo para probar el visor |
| `casebook mcp` | El servidor MCP por stdio; lo levanta el agente |

**Sin pack:** `casebook source add` pregunta el tipo, el proveedor y sus campos; `casebook pack init mi-equipo
--name mi-equipo` arma uno para compartir.

**Login sin fricción:** para las fuentes que inician sesión en el navegador (la consola de Dashboards, por
ejemplo), la extensión de Chrome entrega la sesión sola. Pide permiso solo para los dominios de tus fuentes.
Detalle en [Login y diagnóstico](docs/login-y-doctor.md#extensión-de-chrome).

## Documentación

| Página | Contenido |
|---|---|
| [Instalación](docs/instalacion.md) | Requisitos, qué hace `install.sh`, agentes soportados, modo desarrollo y opciones |
| [Configuración](docs/configuracion.md) | `config.toml`, fuentes, proveedores, textos y la vista Ajustes |
| [Packs](docs/packs.md) | La configuración de un equipo: instalar, armar y compartir un pack |
| [Login y diagnóstico](docs/login-y-doctor.md) | `login` por navegador, `doctor`, túneles y qué significa cada error |
| [Uso del visor y las sesiones](docs/uso.md) | Visor, `query_id`, aprobaciones, búsqueda, retomar investigaciones y comandos |
| [Conocimiento entre investigaciones](docs/conocimiento.md) | `knowledge.db`, estados, vista Conocimiento, compartirlo (archivo o rama del equipo) y respaldo |
| [Seguridad](docs/seguridad.md) | Solo lectura, guardas de MySQL, costo de CloudWatch, redacción y credenciales |

Un proveedor nuevo (otro backend de logs, otra base) se suma como plugin de Python, sin tocar este repo: ver
[Configuración → Proveedores](docs/configuracion.md#proveedores).

## Tests

```bash
.venv/bin/pytest -q
```

## Licencia

[MIT](LICENSE).
