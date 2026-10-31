# Packs

Un pack es la configuración de un equipo, aparte de la app: las fuentes (OpenSearch, CloudWatch, MySQL), el canal de
Slack y su triage, los textos para Claude, las skills y conocimiento semilla. La app sin pack arranca sin fuentes.
`casebook pack init` arma uno de ejemplo, y en [`examples/pack-demo`](../examples/pack-demo) hay uno que funciona
con un OpenSearch local.

## Comandos

```bash
casebook pack                       # el activo: fuentes y skills
casebook pack list                  # los instalados (* = activo) y de dónde salieron
casebook pack install <origen>      # una carpeta, un .zip o una URL git (git@…, ssh://…, https://….git)
casebook pack update                # vuelve a instalar el activo desde donde salió
casebook pack remove <nombre>
casebook pack init <carpeta> --name acme   # uno de ejemplo, comentado
casebook pack build <carpeta> -o acme.zip  # el zip para compartir (valida antes)
casebook pack export -o equipo.zip --name equipo   # tu configuración actual (pack + config.toml) como pack
```

`pack install` muestra un resumen antes de aplicar: fuentes, skills, conocimiento semilla, dominios (la extensión
pide permiso para ellos) y plugins. Sobre un pack ya instalado dice qué cambia, y uno más viejo que el instalado
pide confirmación. Con `--yes` no pregunta, pero no instala plugins.

## Formato

```
acme/
├── pack.toml            mismo formato que config.toml, más [pack]
├── skills/<nombre>/     SKILL.md y sus archivos: se instalan en los agentes con `casebook setup`
└── knowledge/*.json     conocimiento semilla
```

```toml
[pack]
name = "acme"            # minúsculas, números, - o _
version = "1.2.0"
display_name = "Acme"
requires = ">=0.4.0"     # opcional: versión mínima de la app (0.4.0 para varios entornos de MySQL o CloudWatch)

[[requires.plugins]]     # opcional: proveedores que no vienen con la app (código)
name = "acme-logs"
provides = ["acme-logs"]
path = "plugins/acme-logs"   # dentro del zip de `pack build`; o pip = "git+ssh://…"
```

El resto de las tablas son las de `config.toml` ([Configuración](configuracion.md)): `[sources.*]`, `[slack]`,
`[slack.triage]`, `[slack.code]`, `[texts]`, `[card]` y `[doctor]`.

`[slack.code]` le dice al asistente de instalación cómo reconocer el repo de código del equipo, para proponérselo a
cada persona (la ruta es de cada máquina y no va en el pack):

```toml
[slack.code]
label = "el repo backend"          # cómo se le dice en el asistente
hint = "la carpeta que tiene src/ y README.md"
names = ["backend", "api"]         # nombres que puede tener la carpeta
markers = ["src", "README.md"]     # lo que tiene que contener
```

Cada archivo de `knowledge/` es una lista de entradas con `source` (`mysql`, `opensearch` o `cloudwatch`),
`environment`, `entity`, `kind`, `title`, `content` y `topics`. Se importan como `pending` y nunca pisan una entrada
que ya existe.

Para compartir con el equipo, un repo privado con `pack/` (el pack) y `plugins/` (si hace falta) al lado:
`pack build` arma desde ahí un zip con esas dos carpetas, y `pack install` acepta tanto el zip como el repo.

## Seguridad

- El pack es declarativo: no se ejecuta nada de lo que trae. Los archivos se copian sin permiso de ejecución.
- Se rechazan zips con rutas fuera de la carpeta (`..`, rutas absolutas), symlinks, tipos de archivo que no son
  de un pack, y archivos de más de 5 MB (50 MB en total).
- Un plugin es código: solo se instala (con `pip`, en el entorno de la app) si lo confirmas aparte. Sin él, sus
  fuentes quedan deshabilitadas y el doctor dice qué falta.
- Un pack no lleva credenciales: las fuentes dicen de dónde salen (keyring, archivo o variables de entorno). Sí
  puede llevar endpoints internos: compártelo como un documento interno.
