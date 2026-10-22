"""python -m casebook.setup: elige el asistente (gráfico o terminal) y lo corre.

install.sh lo llama después de crear el venv con --repo, --venv y --dev; `casebook setup` lo llama
igual a partir de install-info. Sin --gui ni --consola, pregunta (si hay terminal interactiva y pantalla).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import steps

GUI_REQUIREMENT = "PySide6-Essentials>=6.6"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="casebook setup", description="Asistente de instalación.")
    parser.add_argument("--repo", type=Path, help="el repo desde el que se instala (skills)")
    parser.add_argument("--venv", type=Path, default=Path(sys.prefix))
    parser.add_argument("--dev", action="store_true", help="modo desarrollo (editable desde el repo)")
    parser.add_argument("--agents", help="claude,codex,opencode: en qué agentes registrar, sin preguntar")
    parser.add_argument("--skip-mcp", action="store_true")
    parser.add_argument("--skip-skill", action="store_true")
    parser.add_argument("--no-login", action="store_true", help="sin el paso de inicio de sesión")
    parser.add_argument("--preview", action="store_true", help="muestra el asistente sin cambiar nada")
    ui = parser.add_mutually_exclusive_group()
    ui.add_argument("--gui", action="store_true", help="instalador gráfico")
    ui.add_argument("--consola", action="store_true", help="seguir en la terminal")
    parser.add_argument("--screenshot", help=argparse.SUPPRESS)
    parser.add_argument("--page", type=int, default=0, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def context(args: argparse.Namespace) -> steps.Context:
    agents = None
    if args.agents is not None:
        known = {a for a, _ in steps.AGENTS}
        agents = [a for a in args.agents.split(",") if a]
        unknown = [a for a in agents if a not in known]
        if unknown:
            raise SystemExit(f"agente desconocido: {', '.join(unknown)} (usa claude, codex u opencode)")
    return steps.Context(repo=args.repo, dev=args.dev, venv=args.venv, agents=agents, skip_mcp=args.skip_mcp,
                         skip_skill=args.skip_skill, no_login=args.no_login, dry_run=args.preview)


def graphical() -> bool:
    return sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def has_gui() -> bool:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


def install_gui(console) -> bool:
    """PySide6 va aparte (~110 MB): solo se descarga si eliges el instalador gráfico."""
    with console.status("[bold]Descargando el instalador gráfico[/] (PySide6, ~110 MB; solo la primera vez)…"):
        result = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", GUI_REQUIREMENT],
                                capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[yellow]![/] No pude instalar PySide6: {result.stderr.strip().splitlines()[-1:] or '?'}")
        return False
    return True


def choose_ui(args: argparse.Namespace, console) -> str:
    if args.gui:
        return "gui"
    if args.consola or not interactive() or not graphical():
        return "console"
    import questionary

    from .console import QUESTION_STYLE

    answer = questionary.select(
        "¿Cómo quieres seguir con la instalación?",
        choices=[
            questionary.Choice("Instalador gráfico (recomendado)", "gui"),
            questionary.Choice("Terminal", "console"),
        ],
        qmark="?", pointer="❯", style=QUESTION_STYLE, instruction="(↑/↓ y Enter)",
    ).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def main(argv: list[str] | None = None) -> int:
    from rich.console import Console

    args = parse_args(argv)
    ctx = context(args)
    console = Console(highlight=False)
    try:
        ui = choose_ui(args, console)
        if ui == "gui" and not graphical():
            console.print("[yellow]![/] No hay sesión gráfica: sigo en la terminal.")
            ui = "console"
        if ui == "gui" and not has_gui() and not install_gui(console):
            console.print("Sigo en la terminal.")
            ui = "console"
        if ui == "gui":
            console.print("[dim]Instalador gráfico abierto. Esta terminal espera a que lo cierres.[/]")
            from . import gui

            return gui.run(ctx, screenshot=args.screenshot, page=args.page)
        from . import console as console_ui

        return console_ui.run(ctx, console)
    except KeyboardInterrupt:
        console.print("\n[dim]Instalación interrumpida. Puedes retomarla con[/] casebook setup")
        return 130


if __name__ == "__main__":
    sys.exit(main())
