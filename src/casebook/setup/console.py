"""Asistente de terminal (rich + questionary): los mismos pasos que el gráfico.

Sin terminal interactiva no pregunta nada: usa los agentes instalados, no abre Chrome ni inicia sesiones, y al
final lista lo que quedó pendiente.
"""

from __future__ import annotations

import sys
import time

import questionary
from rich import box
from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .. import native_host
from . import steps

ACCENT, OK, WARN, FAIL, MUTED, BORDER = "#5b8cff", "#3fcf8e", "#ffb84d", "#ff6b6b", "#8b95a3", "#343b48"
ICONS = {steps.OK: ("✓", OK), steps.WARN: ("!", WARN), steps.FAIL: ("✗", FAIL), steps.INFO: ("i", ACCENT)}
STEPS = ("Entorno", "Configuración", "Agentes", "Requisitos", "Extensión", "Slack", "Sesiones", "Listo")
QUESTION_STYLE = questionary.Style([
    ("qmark", f"fg:{ACCENT} bold"),
    ("question", "bold"),
    ("pointer", f"fg:{ACCENT} bold"),
    ("highlighted", f"fg:{ACCENT} bold"),
    ("selected", f"fg:{OK}"),
    ("instruction", f"fg:{MUTED}"),
    ("answer", f"fg:{ACCENT} bold"),
    ("disabled", f"fg:{MUTED} italic"),
])


def line_parts(index: int, done: list[bool]) -> list[tuple[str, str]]:
    parts = []
    for i in range(len(STEPS)):
        if i < index:
            ok = done[i] if i < len(done) else True
            parts.append(("✓ " if ok else "! ", OK if ok else WARN))
        elif i == index:
            parts.append(("● ", f"bold {ACCENT}"))
        else:
            parts.append(("○ ", MUTED))
    return parts


class Wizard:
    def __init__(self, ctx: steps.Context, console: Console) -> None:
        self.ctx, self.c = ctx, console
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self.pending: list[steps.Item] = []
        self.done: list[bool] = []

    # --- piezas ------------------------------------------------------------------------------------------------

    def header(self) -> None:
        from importlib.metadata import version

        head = Table.grid(expand=True)
        head.add_column()
        head.add_column(justify="right")
        mode = self.ctx.mode_label + (" · vista previa" if self.ctx.dry_run else "")
        head.add_row(Text.assemble(("SUPPORT MCP ", f"bold {ACCENT}"), ("/ DEV TOOLS", MUTED)),
                     Text(f"{mode} · v{version('casebook')}", style=MUTED))
        self.c.print(Panel(head, box=box.ROUNDED, border_style=BORDER, padding=(0, 1)))

    def step(self, index: int, title: str, lead: str) -> None:
        line = Text("  ")
        for i, name in enumerate(STEPS):
            if i < index:
                ok = self.done[i] if i < len(self.done) else True
                line.append(f"{'✓' if ok else '!'} {name}", style=OK if ok else WARN)
            elif i == index:
                line.append(f"● {name}", style=f"bold {ACCENT}")
            else:
                line.append(f"○ {name}", style=MUTED)
            if i < len(STEPS) - 1:
                line.append("  ›  ", style=BORDER)
        if line.cell_len > self.c.width:  # terminal angosta: solo las marcas
            line = Text("  ")
            for part in line_parts(index, self.done):
                line.append(*part)
        self.c.print()
        self.c.print(line)
        self.c.print(Rule(Text(f" Paso {index + 1} de {len(STEPS)} · {title} ", style=f"bold {ACCENT}"),
                          style=BORDER, align="left"))
        self.c.print(Padding(Text(lead, style=MUTED), (0, 2)))
        self.c.print()

    def items(self, items: list[steps.Item], collect: bool = True) -> bool:
        for item in items:
            mark, color = ICONS.get(item.status, ("·", MUTED))
            line = Text.assemble((f"{mark} ", f"bold {color}"), item.text)
            if item.detail:
                line.append(f"  {item.detail}", style=MUTED)
            self.c.print(Padding(line, (0, 2)))
            if collect and item.status in (steps.WARN, steps.FAIL):
                self.pending.append(item)
        return all(i.status in (steps.OK, steps.INFO) for i in items)

    def ask(self, question):
        answer = question.ask()
        if answer is None:
            raise KeyboardInterrupt
        return answer

    # --- pasos -------------------------------------------------------------------------------------------------

    def environment(self) -> None:
        self.step(0, "Entorno", "Python, el paquete y el comando casebook en tu PATH.")
        self.done.append(self.items(steps.environment(self.ctx) + steps.link_cli(self.ctx)))

    def configuration(self) -> None:
        from .. import cli, packs

        self.step(1, "Configuración", "El pack del equipo trae las fuentes (OpenSearch, CloudWatch, MySQL), el canal de "
                                      "Slack y las skills. Sin pack la app queda sin fuentes; se agregan después.")
        pack = packs.active()
        if pack:
            self.items([steps.Item(steps.OK, f"Pack {pack.display_name} {pack.version} activo", str(pack.path.parent))])
        else:
            self.items([steps.Item(steps.INFO, "Sin pack activo")], collect=False)
        if not self.interactive or self.ctx.dry_run:
            self.done.append(True)
            return
        keep = "Seguir con el pack activo" if pack else "Omitir por ahora"
        choice = self.ask(questionary.select(
            "¿Qué hago?", choices=[keep, "Instalar un pack (carpeta, .zip o URL git)", "Agregar una fuente a mano"],
            qmark="?", pointer="❯", style=QUESTION_STYLE))
        if choice.startswith("Instalar"):
            source = self.ask(questionary.path("Carpeta, .zip o URL git del pack:", style=QUESTION_STYLE)).strip()

            def confirm(summary: str) -> bool:
                self.c.print(Padding(Text(summary), (0, 2)))
                return self.ask(questionary.confirm("¿Instalar este pack?", default=True, style=QUESTION_STYLE))

            def confirm_plugin(what: str) -> bool:
                return self.ask(questionary.confirm(f"El pack trae el plugin {what}. Es código y se instala con pip. "
                                                    "¿Instalarlo?", default=False, style=QUESTION_STYLE))

            try:
                pack = packs.install(source, confirm, confirm_plugin,
                                     out=lambda line: self.c.print(Padding(Text(line, style=MUTED), (0, 2))))
                self.items([steps.Item(steps.OK, f"Pack {pack.display_name} {pack.version} instalado y activo")])
            except packs.PackError as exc:
                self.items([steps.Item(steps.WARN, "No se instaló el pack", str(exc))])
        elif choice.startswith("Agregar"):
            cli.main(["source", "add"])
        self.done.append(True)

    def agents(self) -> None:
        self.step(2, "Agentes", "Dónde registrar el servidor MCP y las skills de investigación y triage.")
        if self.ctx.skip_mcp and self.ctx.skip_skill:
            self.c.print(Padding(Text("Omitido (--skip-mcp y --skip-skill).", style=MUTED), (0, 2)))
            self.done.append(True)
            return
        available = steps.agents_available()
        selected = steps.default_agents(self.ctx)
        if self.interactive and self.ctx.agents is None:
            selected = self.ask(questionary.checkbox(
                "¿En qué agentes registro casebook?",
                choices=[questionary.Choice(a["label"] + ("" if a["installed"] else "   (no instalado)"), a["id"],
                                            checked=a["id"] in selected) for a in available],
                qmark="?", pointer="❯", style=QUESTION_STYLE,
                instruction="(↑/↓ mover · espacio marcar · Enter continuar)",
            ))
        if not selected:
            self.c.print(Padding(Text("Ningún agente elegido.", style=MUTED), (0, 2)))
            self.done.append(True)
            return
        with self.c.status("Registrando en los agentes…", spinner="dots"):
            results = steps.register_agents(self.ctx, selected)
        labels = dict(steps.AGENTS)
        ok = True
        for agent, items in results.items():
            self.c.print(Padding(Text(labels[agent], style="bold"), (1 if agent != selected[0] else 0, 2, 0, 2)))
            ok = self.items(items) and ok
        self.done.append(ok)

    def prereqs(self) -> None:
        self.step(3, "Requisitos", "Hosts de ssh, que el servidor SSH responda (VPN o IP permitida), las credenciales y el keyring. "
                                   "Solo se revisa; nada se cambia.")
        with self.c.status("Revisando requisitos… (ssh y alias tardan unos segundos)", spinner="dots"):
            items = steps.prereqs()
        ok = self.items(items)
        if not ok:
            self.c.print(Padding(Text("Si tus hosts se llaman distinto, ajústalos en ~/.config/casebook/"
                                      "config.toml (docs/configuracion.md).", style=MUTED), (1, 2, 0, 2)))
        self.done.append(ok)

    def _profiles_table(self, profiles: list[dict]) -> Table:
        table = Table(box=box.SIMPLE_HEAD, border_style=BORDER, header_style=MUTED, padding=(0, 2))
        table.add_column("Perfil")
        table.add_column("Estado")
        table.add_column("Logins", justify="center")
        states = {"loaded": ("✓ cargada", OK), "other_folder": ("✓ cargada · desde otra carpeta", OK),
                  "disabled": ("! desactivada", WARN), "missing": ("○ falta cargarla", MUTED),
                  "folder_gone": ("! su carpeta ya no existe: vuelve a cargarla", WARN)}
        for p in profiles:
            text, color = states[p["state"]]
            table.add_row(p["name"], Text(text, style=color), Text("●", style=ACCENT) if p["login_profile"] else "")
        return table

    def extension(self) -> None:
        if not steps.login_systems():
            self.step(4, "Extensión de Chrome", "Ninguna fuente inicia sesión con el navegador: no hace falta.")
            self.done.append(True)
            return
        self.step(4, "Extensión de Chrome", "Guarda las sesiones de tus fuentes cuando inicias sesión en tu "
                                            "Chrome de siempre. Chrome no permite instalarla sola: se carga una vez "
                                            "por perfil. Es opcional.")
        self.items(steps.extension_install(self.ctx))
        folder = native_host.extension_dir()
        if folder and not self.ctx.dry_run and steps.copy_to_clipboard(str(folder)):
            self.c.print(Padding(Text("La ruta quedó copiada al portapapeles.", style=MUTED), (0, 2)))
        profiles = steps.extension_profiles()
        if not profiles:
            self.c.print(Padding(Text("No encontré perfiles de Google Chrome.", style=WARN), (1, 2)))
            self.done.append(False)
            return
        self.c.print(Padding(self._profiles_table(profiles), (1, 1, 0, 1)))
        missing = [p for p in profiles if p["state"] not in steps.WORKING]
        if steps.extension_state(profiles)["ok"] or not self.interactive or self.ctx.dry_run:
            self._extension_result(profiles)
            return
        team = steps.team_name()
        recommended = next((p["dir"] for p in missing if team and team in p["name"].lower()), None)
        chosen = self.ask(questionary.checkbox(
            "¿En qué perfiles abro chrome://extensions para cargarla?",
            choices=[questionary.Choice(p["name"] + ("   (recomendado)" if p["dir"] == recommended else ""), p["dir"],
                                        checked=p["dir"] == recommended) for p in missing],
            qmark="?", pointer="❯", style=QUESTION_STYLE, instruction="(espacio marcar · Enter continuar; ninguno = omitir)",
        ))
        if not chosen:
            self._extension_result(profiles)
            return
        steps.remember_login_profile(chosen[0])
        for directory in chosen:
            native_host.open_extensions_page(directory)
        self.c.print(Padding(Panel(Group(
            Text.assemble(("1 ", f"bold {ACCENT}"), "Activa ", ("Modo de desarrollador", "bold"), " (arriba a la derecha)"),
            Text.assemble(("2 ", f"bold {ACCENT}"), "Pulsa ", ("Cargar descomprimida", "bold"),
                          ("  (no «Empaquetar extensión»)", MUTED)),
            Text.assemble(("3 ", f"bold {ACCENT}"), "Ctrl+L, pega la ruta (Ctrl+V) y acepta la carpeta"),
        ), title="En Chrome", title_align="left", border_style=BORDER, box=box.ROUNDED, padding=(0, 2)), (1, 2, 0, 2)))
        names = dict((p["dir"], p["name"]) for p in profiles)
        deadline = time.monotonic() + 300
        try:
            with self.c.status(f"Esperando a que la cargues en {', '.join(names[d] for d in chosen)}…  "
                               f"[{MUTED}]Ctrl+C para omitir[/]", spinner="dots"):
                while time.monotonic() < deadline:
                    current = {p["dir"]: p["state"] for p in steps.extension_profiles()}
                    if all(current.get(d) in steps.WORKING for d in chosen) and native_host.connected():
                        break
                    time.sleep(1)
        except KeyboardInterrupt:
            self.c.print(Padding(Text("Omitido; puedes cargarla después con `casebook extension`.",
                                      style=MUTED), (0, 2)))
        self._extension_result(steps.extension_profiles())

    def _extension_result(self, profiles: list[dict]) -> None:
        working = [p["dir"] for p in profiles if p["state"] in steps.WORKING]
        if working and native_host.login_profile() not in working and not self.ctx.dry_run:
            steps.remember_login_profile(working[0])
        state = steps.extension_state(profiles)
        loaded = state["loaded"]
        if state["ok"]:
            self.items([steps.Item(steps.OK, state["subtitle"].capitalize(),
                                   "conectada" if state["connected"] else "esperando su primer contacto")])
        else:
            self.items([steps.Item(steps.WARN, "La extensión no está cargada en ningún perfil",
                                   "sin ella el login usa una ventana aparte; `casebook extension` cuando quieras")])
        self.done.append(state["ok"])

    def slack(self) -> None:
        self.step(5, "Slack (opcional)", "Para vigilar el canal de soporte desde el visor: Slack de escritorio y el "
                                         "conector de Slack de claude.ai. Solo se revisa; nada se cambia.")
        with self.c.status("Revisando Slack…", spinner="dots"):
            items = steps.slack_checks()
        # Opcional: lo que falta no va a los pendientes del final.
        self.items(items, collect=False)
        self.done.append(True)

    def sessions(self) -> None:
        if not steps.login_systems():
            self.step(6, "Inicio de sesión", "Ninguna fuente inicia sesión con el navegador: no hace falta.")
            self.done.append(True)
            return
        self.step(6, "Inicio de sesión", "MySQL usa las credenciales configuradas. Para OpenSearch y CloudWatch se valida "
                                         "la sesión guardada; Claude nunca ve las cookies.")
        if self.ctx.no_login:
            self.c.print(Padding(Text("Omitido (--no-login).", style=MUTED), (0, 2)))
            self.done.append(True)
            return
        with self.c.status("Validando las sesiones guardadas…", spinner="dots"):
            status = steps.sessions()
        self._sessions_table(status)
        missing = [s for s in status if s["status"] != "ok"]
        if missing and self.interactive and not self.ctx.dry_run:
            chosen = self.ask(questionary.checkbox(
                "¿En cuáles inicias sesión ahora?",
                choices=[questionary.Choice(f"{s['label']}   ({s['description']})", s["system"], checked=False)
                         for s in missing],
                qmark="?", pointer="❯", style=QUESTION_STYLE, instruction="(espacio marcar · Enter continuar; ninguno = después)",
            ))
            for system in chosen:
                self.c.print(Rule(Text(f" {steps.login_command(system)} ", style=MUTED), style=BORDER, align="left"))
                steps.login(system, 300, replace=True)
            if chosen:
                with self.c.status("Validando…", spinner="dots"):
                    status = steps.sessions()
                self.c.print()
                self._sessions_table(status)
        pending = [s for s in status if s["status"] != "ok"]
        for s in pending:
            self.pending.append(steps.Item(steps.WARN, f"{s['label']}: sin sesión activa",
                                           f"corre `{steps.login_command(s['system'])}`"))
        self.done.append(not pending)

    def _sessions_table(self, status: list[dict]) -> None:
        table = Table(box=box.SIMPLE_HEAD, border_style=BORDER, header_style=MUTED, padding=(0, 2))
        table.add_column("Servicio")
        table.add_column("Sesión")
        marks = {"ok": ("✓", OK), "missing": ("○", MUTED), "expired": ("!", FAIL), "unknown": ("!", WARN)}
        for s in status:
            mark, color = marks[s["status"]]
            table.add_row(s["label"], Text(f"{mark} {s['detail']}", style=color))
        self.c.print(Padding(table, (0, 1)))

    def finish(self) -> None:
        self.step(7, "Listo", "casebook quedó instalado.")
        if self.pending:
            self.c.print(Padding(Text(f"{len(self.pending)} pendiente(s):", style=f"bold {WARN}"), (0, 2)))
            self.items(self.pending, collect=False)
        else:
            self.items([steps.Item(steps.OK, "Nada pendiente: todo en orden")], collect=False)
        nxt = Table.grid(padding=(0, 2))
        nxt.add_column(style=f"bold {ACCENT}")
        nxt.add_column()
        nxt.add_row("1", Text.assemble("Reinicia tu agente  ", ("Claude Code: /mcp → reconectar; Codex u OpenCode: sesión nueva", MUTED)))
        nxt.add_row("2", Text.assemble("Pídele ", (f"«investiga {steps.card_example()}»", "bold"), ("  el visor se abre solo", MUTED)))
        nxt.add_row("3", Text.assemble(("casebook doctor", "bold"), ("  revisa el estado cuando quieras", MUTED)))
        nxt.add_row("4", Text.assemble(("casebook start", "bold"), ("  tus investigaciones, en un solo lugar", MUTED)))
        self.c.print(Padding(Panel(nxt, title="Siguientes pasos", title_align="left", border_style=BORDER,
                                   box=box.ROUNDED, padding=(0, 1)), (1, 2, 0, 2)))
        if not self.ctx.dev:
            self.c.print(Padding(Text("Para actualizar: git pull && ./install.sh", style=MUTED), (0, 2)))


def run(ctx: steps.Context, console: Console) -> int:
    import os

    wizard = Wizard(ctx, console)
    if not os.environ.get("CASEBOOK_FROM_INSTALLER"):
        wizard.header()
    for stage in (wizard.environment, wizard.configuration, wizard.agents, wizard.prereqs, wizard.extension,
                  wizard.slack, wizard.sessions):
        stage()
    wizard.finish()
    return 0
