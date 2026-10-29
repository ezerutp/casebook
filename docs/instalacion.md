# Instalación

## Requisitos

- Python 3.12 o superior y un agente: Claude Code, Codex u OpenCode.
- Un keyring del sistema (gnome-keyring o kwallet) para guardar las credenciales.
- Lo que pidan tus fuentes (el pack de tu equipo lo dice, y `casebook doctor` lo revisa):
  - **OpenSearch directo:** la URL del cluster y un usuario o API key (`casebook login os-<entorno>`).
  - **OpenSearch por la consola de Dashboards:** un host en `~/.ssh/config` que haga de túnel al dominio, con
    login por clave sin interacción:

    ```
    Host os_tunnel_prod
        HostName bastion.ejemplo.com
        User <tu usuario>
        LocalForward 9123 vpc-ejemplo.us-east-1.es.amazonaws.com:443
    ```

    No hace falta nginx, mkcert ni `/etc/hosts`: esos solo los necesita el navegador para abrir Dashboards. El
    paquete se conecta directo al túnel y verifica el certificado real.
  - **CloudWatch:** un perfil de AWS con permiso de Logs Insights (`pip install 'casebook[aws]'`), o el
    proveedor de un plugin.
  - **MySQL:** un host de `~/.ssh/config` hacia la red de la base, y usuario y contraseña de la réplica (en un
    archivo, variables de entorno o el keyring: [Configuración](configuracion.md)).

## Instalar

```bash
git clone https://github.com/ezerutp/casebook.git ~/repos/casebook
cd ~/repos/casebook
./install.sh
```

Primero pregunta **cómo instalarlo** (viene marcado el modo de tu última instalación; `--local` o `--dev` eligen
sin preguntar):

- **Instalación local:** una copia propia en `~/.local/share/casebook`; el repo se puede mover o borrar.
- **Modo desarrollo:** editable desde el repo (`.venv` del repo); tus cambios aplican sin reinstalar.

Después prepara lo mínimo (Python 3.12+, el entorno y el paquete) y te pregunta cómo seguir:

- **Instalador gráfico** (recomendado): una ventana con los pasos (PySide6), con tema claro u oscuro según tu
  sistema. Trae sus fuentes (Inter y Source Code Pro). La primera vez descarga PySide6 (~110 MB) en el entorno del
  paquete; si no hay sesión gráfica, sigue en la terminal.
- **Terminal**: los mismos pasos, con menús de flechas y espacio.

`--gui` o `--consola` eligen sin preguntar. Sin terminal interactiva (por ejemplo en CI) sigue en la terminal y no
pregunta nada. `./install.sh --preview` muestra el asistente sin cambiar nada (necesita una instalación previa), y
`casebook setup` lo vuelve a abrir cuando quieras.

El instalador no usa sudo y se puede correr de nuevo cuando quieras. Los pasos:

1. **Bienvenida.** Instala una **copia propia** del paquete en `~/.local/share/casebook/venv` (o editable
   desde el repo con `--dev`). Desde ahí corren el comando y el servidor MCP, así que **el repo se puede mover o
   borrar** sin romper nada. Deja el comando `casebook` en `~/.local/bin`. En el instalador gráfico, aquí
   eliges qué configurar (agentes, extensión, Slack, inicio de sesión): lo que apagues se salta. También puedes
   cambiar de modo: vuelve a correr `./install.sh --dev` o `--local` y abre el asistente de nuevo.
2. **Agentes.** Eliges en cuáles instalarlo: **Claude Code**, **Codex** y **OpenCode** (vienen marcados los que
   tienes instalados; `--agents=claude,codex,opencode` los fija sin preguntar). En cada uno registra el servidor
   MCP e instala las skills del pack activo (antes de este paso el asistente pregunta por el pack: ver
   [Packs](packs.md)). Las instrucciones básicas le llegan al agente con el servidor MCP; las skills del pack
   agregan el método de investigación del equipo, sus recetas y cómo clasificar un pedido del canal de soporte.

   | Agente      | Servidor MCP                                   | Skill                                              |
   |-------------|------------------------------------------------|----------------------------------------------------|
   | Claude Code | `claude mcp add -s user` (todas tus carpetas)  | copia en `~/.claude/skills/`                       |
   | Codex       | `codex mcp add`                                | symlink en `~/.agents/skills/`                     |
   | OpenCode    | `mcp` en `~/.config/opencode/opencode.json`    | symlink en `~/.config/opencode/skills/`            |

   Los symlinks de Codex y OpenCode apuntan a una sola copia en `~/.local/share/casebook/skills/`, que se
   actualiza al reinstalar (en `--dev`, al repo). Si había una copia a mano en `~/.codex/skills/`, la quita para
   que no salga duplicada. En `opencode.json` solo toca la entrada `casebook`; si el archivo tiene
   comentarios no lo modifica y te muestra qué agregar. Lo que no es de esta instalación se avisa y no se toca.
3. **Requisitos.** Revisa los hosts de ssh de tus fuentes, que el servidor SSH responda, las credenciales de la
   base, los alias que pida el pack y el keyring, y avisa lo que falte. No cambia nada. En el instalador gráfico van
   agrupados (conexión, credenciales, sistema), con el comando que lo arregla cuando hay uno; un aviso se puede
   ignorar y deja de salir como pendiente (se recuerda en `~/.local/share/casebook/setup-ignorados.json`).
4. **Extensión de Chrome** (opcional). Registra la [extensión](login-y-doctor.md#extensión-de-chrome), te muestra
   tus perfiles de Chrome y en cuáles está cargada (lo lee de las preferencias de cada perfil y se actualiza solo),
   abre `chrome://extensions` en los que elijas y deja la ruta en el portapapeles. Chrome no permite instalarla
   sin «Cargar descomprimida». Sin ella, el login usa una ventana aparte.
5. **Slack** (opcional). Para [vigilar el canal de soporte](uso.md#vigilar-el-canal-de-soporte) desde el
   visor: revisa que **Slack de escritorio** esté instalado y abierto, y que el **conector de Slack de claude.ai**
   esté conectado (si no, en Claude Code: `/mcp` → claude.ai Slack → Authenticate; no hace falta el admin).
   Solo revisa; lo que falte no queda como pendiente. Aquí también puedes indicar la **carpeta con el código** del
   equipo para que el análisis de Slack la lea (solo lectura; ver [Código local](configuracion.md#código-local-en-el-análisis-de-slack)):
   el asistente la busca en tu carpeta personal con lo que declara el pack (`[slack.code]`: nombre de la carpeta y qué
   tiene que contener), te la propone y avisa si no ve lo que esperaba. Es opcional y se puede omitir; la ruta es de
   cada persona y queda en tu `config.toml`, no en el pack.
6. **Inicio de sesión.** Valida las sesiones guardadas de OpenSearch y CloudWatch y te deja iniciar las que
   falten (un login en curso se puede cancelar). Se salta si ninguna fuente inicia sesión en el navegador.
7. **Listo.** Lo pendiente, si algo, y los siguientes pasos.

**Actualizar:** `git pull && ./install.sh` (vuelve a copiar el paquete y las skills; tus sesiones y credenciales no
se tocan).

**Modo desarrollo:** elegirlo en el menú (o `./install.sh --dev`) instala en modo editable desde el repo (`.venv` del repo, y las skills
enlazadas al repo): tus cambios aplican sin reinstalar, con solo reconectar el MCP. Si venías de la instalación
local, se borra su copia (`~/.local/share/casebook/venv` y sus skills) y los agentes elegidos pasan a
apuntar al repo; la extensión de Chrome cargada desde esa copia hay que volver a cargarla desde el repo (el
asistente lo marca como «su carpeta ya no existe»). Para volver a la copia propia, elige «Instalación local» (o
`./install.sh --local`); el `.venv` del repo queda para los tests.

Otras opciones: `--no-login` (sin el paso de inicio de sesión), `--agents=…`, `--skip-mcp`, `--skip-skill` (ambas
aplican a los agentes elegidos) y `--uninstall`.

**Desinstalar:** `./install.sh --uninstall` detiene los visores y servidores MCP que estén corriendo (los de Claude
Code se cierran) y quita la instalación, el comando, las skills, el registro del MCP en los tres agentes y el native
host de la extensión. Si hay credenciales en el keyring, pregunta si borrarlas. Deja tus sesiones, el conocimiento,
los exports, los logs, la configuración y el `.venv` del repo, y te dice dónde están. `./install.sh --uninstall
--force` borra todo sin preguntar: también las credenciales del keyring, `~/.local/share/casebook`,
`~/.config/casebook` y el `.venv` del repo. En los dos casos, la extensión se quita a mano en
`chrome://extensions`.

Si no iniciaste sesión durante la instalación, quedan dos pasos: [guardar las sesiones y verificar](login-y-doctor.md).
