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
./install.sh          # prepara el paquete y abre el asistente (gráfico o en la terminal)
casebook doctor       # todo en ✓ significa que está listo
```

El asistente pregunta por el pack de tu equipo. Si no tienes uno:

```bash
casebook source add                             # pregunta tipo, proveedor y sus campos
casebook pack init mi-equipo --name mi-equipo   # o arma un pack para compartir
```

Después pídele al agente «investiga SUP-1234»: el visor (http://127.0.0.1:8765) se abre solo y muestra cada
consulta en vivo.

**Login sin fricción:** para las fuentes que inician sesión en el navegador (la consola de Dashboards, por
ejemplo), la extensión de Chrome que instala `./install.sh` entrega la sesión sola. Pide permiso solo para los
dominios de tus fuentes. Detalle en [Login y diagnóstico](docs/login-y-doctor.md#extensión-de-chrome).

**Actualizar:** `git pull && ./install.sh` (tus sesiones, conocimiento y credenciales no se tocan).

## Documentación

| Página | Contenido |
|---|---|
| [Instalación](docs/instalacion.md) | Requisitos, qué hace `install.sh`, agentes soportados, modo desarrollo y opciones |
| [Configuración](docs/configuracion.md) | `config.toml`, fuentes, proveedores, textos y la vista Ajustes |
| [Packs](docs/packs.md) | La configuración de un equipo: instalar, armar y compartir un pack |
| [Login y diagnóstico](docs/login-y-doctor.md) | `login` por navegador, `doctor`, túneles y qué significa cada error |
| [Uso del visor y las sesiones](docs/uso.md) | Visor, `query_id`, aprobaciones, búsqueda, retomar investigaciones y comandos |
| [Conocimiento entre investigaciones](docs/conocimiento.md) | `knowledge.db`, estados, vista Conocimiento y respaldo |
| [Seguridad](docs/seguridad.md) | Solo lectura, guardas de MySQL, costo de CloudWatch, redacción y credenciales |

Un proveedor nuevo (otro backend de logs, otra base) se suma como plugin de Python, sin tocar este repo: ver
[Configuración → Proveedores](docs/configuracion.md#proveedores).

## Tests

```bash
.venv/bin/pytest -q
```

## Licencia

[MIT](LICENSE).
