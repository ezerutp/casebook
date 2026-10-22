#!/usr/bin/env bash
# Instala casebook para el usuario actual (sin sudo). Se puede correr de nuevo: es idempotente.
#
# Este script prepara lo mínimo (Python, el venv y el paquete) y después abre el asistente: gráfico (una
# ventana con los pasos) o en la terminal, a elección. El asistente registra el comando, los agentes, revisa los
# requisitos, la extensión de Chrome, Slack (opcional) y las sesiones. Se puede repetir después con `casebook setup`.
#
#   ./install.sh               instala o actualiza una copia propia en ~/.local/share/casebook/venv;
#                              después el repo se puede mover o borrar. Para actualizar: git pull && ./install.sh
#   ./install.sh --dev         modo desarrollo: editable desde este repo (tus cambios aplican sin reinstalar)
#   ./install.sh --local       instalación local, sin preguntar (sin --dev ni --local, se elige en un menú)
#   ./install.sh --gui         el asistente gráfico, sin preguntar (descarga PySide6 la primera vez, ~110 MB)
#   ./install.sh --consola     el asistente en la terminal, sin preguntar
#   ./install.sh --agents=claude,codex,opencode
#                              en qué agentes registrar el MCP y las skills, sin preguntar
#   ./install.sh --skip-mcp    sin registrar el servidor MCP en los agentes
#   ./install.sh --skip-skill  sin instalar las skills en los agentes
#   ./install.sh --no-login    sin el paso de inicio de sesión
#   ./install.sh --preview     muestra el asistente sin modificar tu instalación (necesita una instalación previa)
#   ./install.sh --uninstall   detiene los procesos y quita la instalación, el comando, las skills y el registro del
#                              MCP en todos los agentes; pregunta si borrar las credenciales del keyring y deja tus
#                              sesiones, conocimiento, exports, logs y config
#   ./install.sh --uninstall --force
#                              borra todo sin preguntar: también credenciales, bases, exports, logs, config y el
#                              entorno de desarrollo del repo
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/casebook"
APP_VENV="$APP_DIR/venv"
DEV_VENV="$REPO/.venv"
INFO_FILE="$APP_DIR/install-info"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/casebook"
BIN_DIR="$HOME/.local/bin"
CLI_LINK="$BIN_DIR/casebook"
# Las skills vienen del pack: para desinstalar se juntan las de los packs instalados y las del repo.
SKILL_NAMES=()
for d in "${XDG_DATA_HOME:-$HOME/.local/share}"/casebook/packs/*/skills/*/ "$(cd "$(dirname "$0")" && pwd)"/packs/*/skills/*/; do
    if [[ -f "$d/SKILL.md" ]]; then SKILL_NAMES+=("$(basename "$d")"); fi
done
if [[ ${#SKILL_NAMES[@]} -gt 0 ]]; then mapfile -t SKILL_NAMES < <(printf '%s\n' "${SKILL_NAMES[@]}" | sort -u); fi
CLAUDE_SKILLS="$HOME/.claude/skills"
MCP_NAME="casebook"
CODEX_SKILLS="$HOME/.agents/skills"
CODEX_OLD_SKILLS="${CODEX_HOME:-$HOME/.codex}/skills"  # ubicación anterior (copia a mano)
OPENCODE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
OPENCODE_CONFIG="$OPENCODE_DIR/opencode.json"
OPENCODE_SKILLS="$OPENCODE_DIR/skills"

B=; G=; Y=; R=; C=; D=; N=
if [[ -t 1 && ${TERM:-dumb} != dumb && -z ${NO_COLOR+x} ]]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'
    C=$'\033[38;2;91;140;255m'; D=$'\033[2m'; N=$'\033[0m'
fi
banner() {
    printf '\n  %sSUPPORT MCP%s %s/ DEV TOOLS%s\n' "$B$C" "$N" "$D" "$N"
    printf '  %sOpenSearch · CloudWatch · MySQL%s\n' "$D" "$N"
}
step() { printf '\n  %s●%s %s%s%s\n' "$C" "$N" "$B" "$*" "$N"; }
ok()   { printf '    %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '    %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n    %s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
# Corre un comando largo con un spinner; si falla, muestra su salida.
spin() {  # spin "mensaje" comando...
    local msg="$1" log pid frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
    shift
    log="$(mktemp)"
    "$@" >"$log" 2>&1 &
    pid=$!
    if [[ -t 1 ]]; then
        while kill -0 "$pid" 2>/dev/null; do
            printf '\r    %s%s%s %s' "$C" "${frames:i++%${#frames}:1}" "$N" "$msg"
            sleep 0.1
        done
        printf '\r\033[2K'
    fi
    if wait "$pid"; then rm -f "$log"; return 0; fi
    printf '    %s✗%s %s\n' "$R" "$N" "$msg"
    tail -n 20 "$log" | sed 's/^/      /' >&2
    rm -f "$log"
    return 1
}

# Menú de una opción con flechas (bash 3.2 de macOS: sin arrays asociativos). Deja el índice en CHOICE.
choose() {  # choose <índice por defecto> "opción|detalle" ...
    local cursor="$1" key rest i n
    shift
    n=$#
    local options=("$@")
    while true; do
        for i in "${!options[@]}"; do
            if [[ $i -eq $cursor ]]; then printf '    %s❯ %s%s  %s%s%s
' "$B$C" "${options[$i]%%|*}" "$N" "$D" "${options[$i]#*|}" "$N"
            else printf '      %s  %s%s%s
' "${options[$i]%%|*}" "$D" "${options[$i]#*|}" "$N"; fi
        done
        IFS= read -rsn1 key </dev/tty || key=""
        case "$key" in
            "") CHOICE=$cursor; return 0 ;;
            [1-9]) if [[ $key -le $n ]]; then CHOICE=$((key - 1)); return 0; fi ;;
            $'\033')
                rest=""
                read -rsn2 -t 1 rest </dev/tty || true
                case "$rest" in
                    '[A'|'OA') cursor=$(((cursor + n - 1) % n)) ;;
                    '[B'|'OB') cursor=$(((cursor + 1) % n)) ;;
                esac
                ;;
        esac
        for i in "${!options[@]}"; do printf '\033[1A\033[2K'; done
    done
}

DEV=0; MODE_SET=0; UNINSTALL=0; PREVIEW=0; FORCE=0
SETUP_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --dev) DEV=1; MODE_SET=1 ;;
        --local) DEV=0; MODE_SET=1 ;;
        --gui|--consola|--skip-mcp|--skip-skill|--no-login) SETUP_ARGS+=("$arg") ;;
        --agents=*) SETUP_ARGS+=(--agents "${arg#--agents=}") ;;
        --browser) : ;;  # compatibilidad: el login siempre usa Chrome
        --preview) PREVIEW=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "opción desconocida: $arg (usa --help)" ;;
    esac
done
[[ $FORCE -eq 0 || $UNINSTALL -eq 1 ]] || die "--force solo va con --uninstall"

# Sin --dev ni --local, se pregunta (con terminal interactiva); viene marcado el modo de la última instalación.
if [[ $MODE_SET -eq 0 && $UNINSTALL -eq 0 && $PREVIEW -eq 0 && -t 0 && -t 1 ]] && ( : </dev/tty ) 2>/dev/null; then
    banner
    last=0
    if grep -qx "mode=dev" "$INFO_FILE" 2>/dev/null; then last=1; fi
    printf '\n  %s?%s %s¿Cómo lo instalo?%s %s(↑/↓ y Enter)%s\n' "$C" "$N" "$B" "$N" "$D" "$N"
    choose "$last" \
        "Instalación local|copia propia en $APP_DIR; el repo se puede mover o borrar" \
        "Modo desarrollo|editable desde este repo; tus cambios aplican sin reinstalar"
    DEV=$CHOICE
    SHOWN_BANNER=1
fi

if [[ $DEV -eq 1 ]]; then VENV="$DEV_VENV"; else VENV="$APP_VENV"; fi
CLI_TARGET="$VENV/bin/casebook"
MODE_ARGS=(--repo "$REPO" --venv "$VENV")
if [[ $DEV -eq 1 ]]; then MODE_ARGS+=(--dev); fi

# Algo es "nuestro" (se puede reemplazar o quitar) si apunta a este repo o a una instalación de casebook.
# Algo es "nuestro" (se puede reemplazar o quitar) si apunta a este repo o a una instalación de casebook.
ours_link() {
    [[ -L "$1" ]] || return 1
    local t; t="$(readlink "$1")"
    [[ "$t" == "$REPO"* || "$t" == "$APP_DIR"* || "$t" == */casebook/* ]]
}
ours_skill() {  # el symlink del modo --dev, o una copia con nuestro SKILL.md (la carpeta se llama como la skill)
    ours_link "$1" || grep -qx "name: $(basename "$1")" "$1/SKILL.md" 2>/dev/null
}
ours_agent_skill() {  # además, el symlink a la skill de Claude Code de una versión anterior del instalador
    [[ -L "$1" && "$(readlink "$1")" == "$CLAUDE_SKILLS/$(basename "$1")" ]] || ours_skill "$1"
}
remove_agent_skill() {  # remove_agent_skill <ruta> <agente>
    [[ -e "$1" || -L "$1" ]] || return 0
    if ours_agent_skill "$1"; then rm -rf "$1"; ok "quitada la skill de $2 ($1)"
    else warn "$1 no es de esta instalación; no lo toco"; fi
}
# Lee o cambia mcp.<nombre> en opencode.json sin tocar el resto. Falla (y se avisa) si el archivo no es JSON
# estricto, por ejemplo con comentarios.
opencode_mcp() {  # opencode_mcp get|set|remove
    "${PY:-python3}" - "$1" "$OPENCODE_CONFIG" "$MCP_NAME" "$CLI_TARGET" <<'PYEOF'
import json, os, sys
action, path, name, target = sys.argv[1:5]
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {"$schema": "https://opencode.ai/config.json"}
mcp = data.get("mcp") or {}
if action == "get":
    print(" ".join((mcp.get(name) or {}).get("command") or []))
    sys.exit(0)
if action == "set":
    mcp[name] = {"type": "local", "command": [target, "mcp"], "enabled": True}
elif name in mcp:
    del mcp[name]
else:
    sys.exit(0)
data["mcp"] = mcp
if not mcp:
    del data["mcp"]
    if set(data) <= {"$schema"}:  # solo quedaba lo que agregamos nosotros
        os.remove(path)
        sys.exit(0)
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, path)
PYEOF
}
codex_mcp_command() {  # el comando con que está registrado el MCP en Codex (vacío si no está)
    codex mcp get "$MCP_NAME" --json 2>/dev/null \
        | "${PY:-python3}" -c 'import json,sys; print(json.load(sys.stdin)["transport"].get("command") or "")' 2>/dev/null \
        || true
}

# --- desinstalar ---------------------------------------------------------------------------------------------
if [[ $UNINSTALL -eq 1 ]]; then
    step "Desinstalando"
    CLI=""
    for v in "$APP_VENV" "$DEV_VENV"; do
        if [[ -x "$v/bin/casebook" ]]; then CLI="$v/bin/casebook"; break; fi
    done
    if [[ -n "$CLI" ]]; then
        # Los visores y MCP que siguen corriendo usarían archivos que se van a borrar.
        if "$CLI" stop all >/dev/null 2>&1; then ok "detenidos los visores y servidores MCP en marcha"
        else warn "no pude detener todos los procesos; revisa con: $CLI stop all"; fi
        stored="$("${CLI%/*}/python" -c 'from casebook import config, credentials as c, systems
print(" ".join(s.id for s in systems.all_systems(config.load()) if c.keyring.get_password(c.SERVICE, s.keyring)))' 2>/dev/null || true)"
        if [[ -n "$stored" ]]; then
            answer=n
            if [[ $FORCE -eq 1 ]]; then answer=s
            elif ( : </dev/tty ) 2>/dev/null; then
                printf '    %s?%s ¿Borro también las credenciales del keyring (%s)? [s/N] ' "$C" "$N" "$stored"
                read -r answer </dev/tty || answer=n
            fi
            if [[ "$answer" == [sSyY]* ]]; then
                for system in $stored; do "$CLI" logout "$system" >/dev/null; done
                ok "credenciales borradas del keyring ($stored)"
            else
                warn "quedan credenciales en el keyring ($stored); se borran con ./install.sh --uninstall --force"
            fi
        fi
    fi
    if ours_link "$CLI_LINK"; then rm "$CLI_LINK"; ok "quitado $CLI_LINK"
    elif [[ -e "$CLI_LINK" ]]; then warn "$CLI_LINK no es de esta instalación; no lo toco"; fi
    for name in "${SKILL_NAMES[@]}"; do
        dst="$CLAUDE_SKILLS/$name"
        [[ -e "$dst" || -L "$dst" ]] || continue
        if ours_skill "$dst"; then rm -rf "$dst"; ok "quitada la skill $dst"
        else warn "$dst no es de esta instalación; no lo toco"; fi
    done
    if command -v claude >/dev/null && claude mcp get "$MCP_NAME" >/dev/null 2>&1; then
        claude mcp remove -s user "$MCP_NAME" >/dev/null 2>&1 && ok "servidor MCP quitado de Claude Code" \
            || warn "no pude quitar el MCP; corre: claude mcp remove -s user $MCP_NAME"
    fi
    for name in "${SKILL_NAMES[@]}"; do
        remove_agent_skill "$CODEX_SKILLS/$name" Codex
        remove_agent_skill "$CODEX_OLD_SKILLS/$name" Codex
    done
    if command -v codex >/dev/null && codex mcp get "$MCP_NAME" >/dev/null 2>&1; then
        codex mcp remove "$MCP_NAME" >/dev/null 2>&1 && ok "servidor MCP quitado de Codex" \
            || warn "no pude quitar el MCP de Codex; corre: codex mcp remove $MCP_NAME"
    fi
    for name in "${SKILL_NAMES[@]}"; do remove_agent_skill "$OPENCODE_SKILLS/$name" OpenCode; done
    if [[ -f "$OPENCODE_CONFIG" ]]; then
        if [[ -n "$(opencode_mcp get 2>/dev/null)" ]]; then
            opencode_mcp remove && ok "servidor MCP quitado de OpenCode" \
                || warn "no pude quitar el MCP de OpenCode; borra mcp.$MCP_NAME de $OPENCODE_CONFIG"
        fi
    fi
    rmdir "$CODEX_SKILLS" "$OPENCODE_SKILLS" "$OPENCODE_DIR" 2>/dev/null || true  # solo si quedaron vacías
    if [[ -n "$CLI" ]]; then
        "$CLI" extension uninstall >/dev/null 2>&1 \
            && ok "native host de la extensión quitado (quita la extensión en chrome://extensions)"
    fi
    if [[ -d "$APP_DIR/skills" ]]; then rm -rf "$APP_DIR/skills"; ok "quitadas las skills compartidas ($APP_DIR/skills)"; fi
    if [[ -d "$APP_VENV" ]]; then rm -rf "$APP_VENV"; ok "quitada la instalación ($APP_VENV)"; fi
    rm -f "$INFO_FILE"
    if [[ $FORCE -eq 1 ]]; then
        if [[ -d "$APP_DIR" ]]; then rm -rf "$APP_DIR"; ok "borrados sesiones, conocimiento, exports y logs ($APP_DIR)"; fi
        if [[ -d "$CONFIG_DIR" ]]; then rm -rf "$CONFIG_DIR"; ok "borrada la configuración ($CONFIG_DIR)"; fi
        if [[ -d "$DEV_VENV" ]]; then rm -rf "$DEV_VENV"; ok "borrado el entorno de desarrollo del repo ($DEV_VENV)"; fi
        exit 0
    fi
    left=()
    for f in sessions.db knowledge.db exports slack-watch.json slack-watch; do
        if [[ -e "$APP_DIR/$f" ]]; then left+=("$f"); fi
    done
    if compgen -G "$APP_DIR/tunnel-*.log" >/dev/null; then left+=("tunnel-*.log"); fi
    if [[ ${#left[@]} -gt 0 || -d "$CONFIG_DIR" || -d "$DEV_VENV" ]]; then
        echo
        echo "Quedan, por si los quieres borrar a mano (o corre ./install.sh --uninstall --force):"
        if [[ ${#left[@]} -gt 0 ]]; then
            echo "  - sesiones, conocimiento y logs: $APP_DIR (${left[*]})"
        fi
        if [[ -d "$CONFIG_DIR" ]]; then echo "  - la configuración: $CONFIG_DIR"; fi
        if [[ -d "$DEV_VENV" ]]; then echo "  - el entorno de desarrollo del repo: rm -rf $DEV_VENV"; fi
    fi
    exit 0
fi

# --- vista previa --------------------------------------------------------------------------------------------
if [[ $PREVIEW -eq 1 ]]; then
    for v in "$VENV" "$DEV_VENV" "$APP_VENV"; do
        if [[ -x "$v/bin/python" ]] && "$v/bin/python" -c 'import casebook.setup, rich, questionary' 2>/dev/null; then
            exec "$v/bin/python" -m casebook.setup "${MODE_ARGS[@]}" --preview ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}
        fi
    done
    die "la vista previa necesita una instalación previa: corre ./install.sh (o ./install.sh --dev)"
fi

[[ ${SHOWN_BANNER:-0} -eq 1 ]] || banner
if [[ $DEV -eq 1 ]]; then printf '\n  Modo %sdesarrollo%s: editable desde %s\n' "$C" "$N" "$REPO"
else printf '\n  Modo %sinstalación local%s: copia propia en %s\n' "$C" "$N" "$APP_DIR"; fi

# --- python --------------------------------------------------------------------------------------------------
step "Python"
PY=""
for candidate in python3 python3.14 python3.13 python3.12; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
        PY="$(command -v "$candidate")"; break
    fi
done
[[ -n "$PY" ]] || die "hace falta Python 3.12 o superior"
ok "$("$PY" --version) ($PY)"

# --- entorno y paquete ---------------------------------------------------------------------------------------
step "Paquete"
if [[ ! -x "$VENV/bin/python" ]]; then
    mkdir -p "$(dirname "$VENV")"
    spin "Creando el entorno en $VENV" "$PY" -m venv "$VENV" \
        || die "no pude crear el venv (en Debian/Ubuntu: sudo apt install python3-venv)"
    ok "entorno creado en $VENV"
else
    ok "entorno en $VENV"
fi
spin "Actualizando pip" "$VENV/bin/python" -m pip install --quiet --upgrade pip || die "falló pip"
if [[ $DEV -eq 1 ]]; then
    spin "Instalando dependencias (modo editable)" "$VENV/bin/python" -m pip install --quiet -e "$REPO[dev]" \
        || die "falló pip install"
    ok "instalado en modo editable: tus cambios en $REPO aplican sin reinstalar"
else
    # Primero las dependencias; después el paquete forzado, para que una versión igual con cambios se actualice.
    spin "Instalando dependencias" "$VENV/bin/python" -m pip install --quiet "$REPO" || die "falló pip install"
    spin "Copiando el paquete" "$VENV/bin/python" -m pip install --quiet --force-reinstall --no-deps "$REPO" \
        || die "falló pip install"
    VERSION="$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("casebook"))')"
    ok "casebook $VERSION en $VENV (ya no depende del repo)"
fi
mkdir -p "$APP_DIR"
{
    echo "mode=$([[ $DEV -eq 1 ]] && echo dev || echo copia)"
    echo "venv=$VENV"
    echo "source=$REPO"
    echo "installed_at=$(date -Iseconds)"
} > "$INFO_FILE"
# Al pasar de la instalación local a --dev, su copia ya no la usa nadie: el asistente registra los agentes con el
# repo. El native host se reescribe ya (y, en modo dev, borra la copia de la extensión) para no apuntar a lo borrado.
if [[ $DEV -eq 1 && -d "$APP_VENV" ]]; then
    if [[ -e "$APP_DIR/native-host" ]]; then "$CLI_TARGET" extension install --no-abrir </dev/null >/dev/null 2>&1 || true; fi
    rm -rf "$APP_VENV" "$APP_DIR/skills"
    ok "quitada la instalación local anterior ($APP_VENV)"
fi

# --- pack ----------------------------------------------------------------------------------------------------
# La configuración del equipo (fuentes, Slack, skills) vive en un pack. Sin pack, el asistente ofrece instalar uno.
# Si ya hay uno activo que vino de una carpeta o un repo git, se actualiza desde ahí.
if [[ -f "$APP_DIR/packs/active" ]]; then
    if "$CLI_TARGET" pack update --yes >/dev/null 2>&1; then ok "pack $(cat "$APP_DIR/packs/active") actualizado"
    else ok "pack $(cat "$APP_DIR/packs/active") activo (para actualizarlo: casebook pack install <zip, carpeta o URL>)"; fi
fi

# --- asistente -----------------------------------------------------------------------------------------------
# El resto (comando, agentes, requisitos, extensión, sesiones) lo hace el asistente, gráfico o de terminal.
export CASEBOOK_FROM_INSTALLER=1  # el asistente no repite la cabecera
exec "$VENV/bin/python" -m casebook.setup "${MODE_ARGS[@]}" ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}
