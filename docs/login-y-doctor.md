# Login y diagnóstico

## Extensión de Chrome

Para las fuentes que inician sesión en el navegador (la consola de OpenSearch Dashboards, o el proveedor de un
plugin), con la extensión inicias sesión en **tu Chrome de siempre** (con tus contraseñas guardadas) y la sesión
llega sola al keyring. Si una sesión vence, entra a la página en tu Chrome y listo.

```bash
casebook extension     # registra el native host y ofrece abrir chrome://extensions en tus perfiles
```

Después, en cada perfil donde inicias sesión: `chrome://extensions` → «Modo de desarrollador» → «Cargar
descomprimida» → la carpeta que te indica el comando. Igual que el paquete:

- **Instalación local** (`./install.sh`): una copia propia en `~/.local/share/casebook/chrome-extension`;
  el repo se puede mover o borrar. En el selector de carpetas, `Ctrl+L` y pega la ruta (o `Ctrl+H` para ver las
  carpetas ocultas).
- **Modo desarrollo** (`./install.sh --dev`): [`chrome-extension/`](../chrome-extension) del repo; tus cambios
  aplican con ⟳.

El instalador te abre esa página en los perfiles que elijas; para agregar otro perfil, vuelve a correr el
comando. `login` abre las páginas en el primer perfil que elegiste. Si cambias de modo, quita la extensión y
cárgala de nuevo desde la carpeta nueva (el ID es el mismo).

Cómo funciona:

- La extensión no trae dominios fijos: el native host le dice cuáles usa cada fuente (salen del pack del equipo) y
  ella pide permiso para esos, y solo para esos. La primera vez (y cuando el pack suma una fuente), abre la
  ventanita de la extensión y toca **Dar permiso**. Hasta entonces Chrome no le deja leer esas sesiones; `doctor`
  y el instalador avisan si falta.
- Cuando cambian las cookies de una de esas fuentes, o cuando termina de cargar una de sus páginas, la extensión
  manda al native host (un programa del paquete que lanza Chrome) las cookies que Chrome enviaría a la URL de
  validación. Lo hace como mucho una vez cada 15 segundos por servicio.
- El host las valida contra la API, igual que `login`, y solo si las acepta las guarda en el keyring. Una cookie
  rechazada (por ejemplo, un login a medio completar) no pisa la que tienes guardada.
- Con la extensión conectada, `casebook login <id>` abre la página en tu Chrome y espera a que llegue la
  sesión. `--ventana-separada` usa la ventana aparte de abajo.
- El ícono de la llave en la barra de Chrome abre una ventanita con el estado de cada servicio (cuándo se guardó
  la sesión o por qué no validó) y un botón «Enviar sesiones ahora». `doctor` muestra lo mismo en la terminal.
  El ícono es provisional: la fuente está en `chrome-extension/icons/icon.svg`.
- Después de actualizar (`git pull && ./install.sh`), recarga la extensión en `chrome://extensions` (⟳).
  `casebook extension
  uninstall` quita el native host; la extensión se quita en `chrome://extensions`.

## Ventana aparte (sin extensión)

Sin la extensión, cada asistente abre Chrome para que completes el login y MFA, valida la sesión contra la API y la guarda en
el keyring del sistema. Córrelos en tu terminal. El copiado y pegado de cookies ya no está soportado.

```bash
casebook source list            # los ids de tus fuentes y cómo inicia sesión cada una
casebook login os               # por ejemplo, el OpenSearch "prod" por la consola de Dashboards
```

Cada fuente dice cómo inicia sesión: en el navegador (lo de esta sección), con usuario y contraseña o API key que
`login <id>` pide y guarda en el keyring (OpenSearch directo), o fuera de casebook (un perfil de AWS para
CloudWatch, el archivo o las variables de entorno de la base).

Requiere Google Chrome instalado y una sesión gráfica. Abre una ventana separada: completa tú el login y MFA.
El programa recoge solo las cookies aplicables a la URL de validación del servicio elegido, las prueba contra su
API y guarda la sesión en el keyring; no imprime cookies ni exporta un archivo de sesión. La ventana se cierra
al terminar. No reutiliza el perfil habitual de Chrome ni sus contraseñas guardadas; tendrás que iniciar sesión
en esta ventana. Después el MCP usa lo guardado en el keyring hasta que venza. No se persiste el perfil de Chrome.
OpenSearch por la consola necesita que la URL de Dashboards cargue en el navegador.

Tienes cinco minutos para completar el login (`--browser-timeout 600` permite diez). Cerrar la ventana o cancelar
con Ctrl+C conserva las credenciales anteriores, salvo las actualizaciones de cookies de una sesión ya validada.
Si falta Chrome o falla la conexión, corrige el requisito indicado y vuelve a ejecutar el comando.
Playwright se instala siempre, también con `--no-login` y en modo desarrollo.
OpenSearch prepara su túnel antes de abrir Dashboards.

**MySQL** usa las credenciales de `[sources.mysql.credentials]` (un archivo o variables de entorno); si no hay,
`casebook login db` las pide y las guarda en el keyring.

Las sesiones vencen (las cookies duran lo que dura tu sesión en el navegador). Te
enteras al abrir una sesión en Claude, porque la tarjeta "Estado de conexiones" lo marca y Claude te dice qué
`login` correr. El servidor de OpenSearch rota las cookies en cada respuesta, y el paquete guarda siempre la
última.

## Verificar

```bash
casebook doctor
```

Todo en ✓ significa que está listo. Si algo falla, el mensaje dice qué correr.

**Túneles:** no hace falta dejar el túnel corriendo. Si el túnel de OpenSearch está caído, el paquete lo levanta
con el host de `~/.ssh/config` de la fuente (por defecto `os_tunnel_<entorno>`). Si está colgado (el puerto acepta conexiones pero nada
responde), termina ese ssh y lo levanta de nuevo. Esto pasa en `doctor`, al abrir una sesión y antes de una
consulta si hace más de un minuto que no se usa. El túnel de MySQL es propio del paquete y se levanta solo.

**Al abrir una sesión** se corre `doctor` automáticamente. El resultado aparece como la tarjeta "Estado de
conexiones" en el visor, y Claude te avisa qué falta (por ejemplo `casebook login os` si venció la
cookie) antes de consultar ese sistema. Las cookies vencidas son lo único que no se arregla solo: requieren tu
login en el navegador.

**Si algo falla, el mensaje dice por qué.** Cuando un túnel no levanta, `doctor` muestra la última línea de ssh y
qué significa. Por ejemplo, `Connection timed out` quiere decir que tu máquina no llega al servidor SSH (falta la
VPN o tu IP no está permitida), y `Permission denied` que la clave no está cargada (`ssh-add`). En la sección
de requisitos, la línea "servidor SSH … responde" es la prueba directa de la red: si dice "no respondió", el
problema no es el paquete sino el acceso al servidor SSH. El detalle de cada intento queda en
`~/.local/share/casebook/tunnel-*.log`.

Siguiente: [Uso del visor y las sesiones](uso.md)
