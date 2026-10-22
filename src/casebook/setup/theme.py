"""Tokens de diseño y la hoja de estilos del instalador gráfico.

Papel cálido, tinta casi negra para lo primario y un solo acento de marca (el paso activo y el punto del logo).
Los estados van con fondo suave. Las fuentes (Inter y Source Code Pro) vienen en setup/fonts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FONTS_DIR = Path(__file__).parent / "fonts"
UI_FONTS = ["Inter", "Cantarell", "Noto Sans", "DejaVu Sans", "sans-serif"]
MONO_FONTS = ["Source Code Pro", "JetBrains Mono", "DejaVu Sans Mono", "monospace"]
RADIUS = 14
RADIUS_SMALL = 9


@dataclass(frozen=True, slots=True)
class Palette:
    name: str
    background: str
    sidebar: str
    surface: str
    sunken: str
    border: str
    border_soft: str
    text: str
    ink: str  # títulos y lo más importante
    muted: str
    faint: str
    primary: str  # botón primario y toggle encendido
    primary_hover: str
    primary_text: str
    brand: str
    brand_soft: str
    neutral_soft: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    warning_panel: str
    danger: str
    danger_soft: str
    toggle_off: str
    toggle_locked: str
    knob: str  # la perilla del toggle apagado
    knob_on: str  # y encendido (en oscuro el riel encendido es claro: la perilla va oscura)
    logo: str  # el fondo del logo
    tooltip: str
    tooltip_text: str
    tooltip_muted: str
    shadow: str


LIGHT = Palette(
    name="light",
    background="#f6f5f1",
    sidebar="#fbfaf7",
    surface="#ffffff",
    sunken="#f1efea",
    border="#e5e2db",
    border_soft="#eeece6",
    text="#2b2a27",
    ink="#1a1917",
    muted="#6e6c66",
    faint="#a3a19a",
    primary="#1a1917",
    primary_hover="#33312d",
    primary_text="#ffffff",
    brand="#ee5b2b",
    brand_soft="#fdebe3",
    neutral_soft="#ecebe6",
    success="#1e8a5a",
    success_soft="#e6f3ec",
    warning="#b26b00",
    warning_soft="#fbf0dc",
    warning_panel="#fffbf3",
    danger="#c93a2e",
    danger_soft="#fbe7e4",
    toggle_off="#d6d3cb",
    toggle_locked="#8c8a84",
    knob="#ffffff",
    knob_on="#ffffff",
    logo="#1a1917",
    tooltip="#1f1e1b",
    tooltip_text="#ffffff",
    tooltip_muted="#d9d6cf",
    shadow="#1a1917",
)

DARK = Palette(
    name="dark",
    background="#161513",
    sidebar="#1a1917",
    surface="#201f1c",
    sunken="#282622",
    border="#33312c",
    border_soft="#2b2925",
    text="#dedbd3",
    ink="#f2f0ea",
    muted="#a19e95",
    faint="#75726b",
    primary="#f2f0ea",
    primary_hover="#ffffff",
    primary_text="#1a1917",
    brand="#f26a3d",
    brand_soft="#3a2219",
    neutral_soft="#2d2b27",
    success="#46b583",
    success_soft="#1b2c23",
    warning="#e0a040",
    warning_soft="#33281a",
    warning_panel="#251f17",
    danger="#ef6a5c",
    danger_soft="#341d1a",
    toggle_off="#45423c",
    toggle_locked="#6d6a63",
    knob="#dedbd3",
    knob_on="#1a1917",
    logo="#33312c",
    tooltip="#f2f0ea",
    tooltip_text="#1a1917",
    tooltip_muted="#4a4843",
    shadow="#000000",
)


def rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {alpha})"


# Estado de un Item (steps.OK, WARN...) -> (color, fondo suave).
def status_colors(palette: Palette, status: str) -> tuple[str, str]:
    return {
        "ok": (palette.success, palette.success_soft),
        "warn": (palette.warning, palette.warning_soft),
        "fail": (palette.danger, palette.danger_soft),
        "brand": (palette.brand, palette.brand_soft),
    }.get(status, (palette.muted, palette.neutral_soft))


def status_color(palette: Palette, status: str) -> str:
    return status_colors(palette, status)[0]


def stylesheet(p: Palette) -> str:
    ui_font = ", ".join(f'"{name}"' for name in UI_FONTS)
    mono_font = ", ".join(f'"{name}"' for name in MONO_FONTS)
    chips = "\n".join(
        f'    #Chip[tone="{tone}"] {{ background: {bg}; }}\n    #Chip[tone="{tone}"] #ChipText {{ color: {fg}; }}'
        for tone, (fg, bg) in (
            ("ok", (p.success, p.success_soft)), ("warn", (p.warning, p.warning_soft)),
            ("fail", (p.danger, p.danger_soft)), ("neutral", (p.muted, p.neutral_soft)),
            ("brand", (p.brand, p.brand_soft)),
        )
    )
    return f"""
    * {{ font-family: {ui_font}; outline: none; }}
    QWidget {{ color: {p.text}; background: transparent; font-size: 13px; }}
    QMainWindow, #Root, #PageHolder {{ background: {p.background}; }}

    /* ---- barra lateral ---- */
    #Sidebar {{ background: {p.sidebar}; border-right: 1px solid {p.border}; }}
    #BrandName {{ font-size: 14px; font-weight: 600; color: {p.ink}; }}
    #BrandSubtitle {{ color: {p.muted}; font-size: 12px; }}
    #SideSection {{ color: {p.faint}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
    #ModeBox {{ background: {p.sunken}; border-radius: 10px; }}
    #ModeText {{ color: {p.ink}; font-size: 12px; font-weight: 600; }}

    /* ---- página ---- */
    #Kicker {{ color: {p.brand}; font-size: 12px; font-weight: 600; }}
    #PageTitle {{ font-size: 26px; font-weight: 600; color: {p.ink}; }}
    #PageLead {{ color: {p.muted}; font-size: 14px; }}
    #Heading {{ color: {p.ink}; font-size: 13px; font-weight: 600; }}
    #HeadingNote {{ color: {p.faint}; font-size: 12px; }}
    #SectionTitle {{ color: {p.faint}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
    #Card {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: {RADIUS}px; }}
    #Banner[tone="warn"] {{ background: {p.warning_soft}; border-radius: 12px; }}
    #Banner[tone="ok"] {{ background: {p.success_soft}; border-radius: 12px; }}
    #Row {{ background: transparent; }}
    #Row[expanded="true"][tone="warn"] {{ background: {p.warning_panel}; border-radius: 12px; }}
    #Divider {{ background: {p.border_soft}; min-height: 1px; max-height: 1px; }}
    #RowTitle {{ color: {p.ink}; font-size: 13px; font-weight: 600; }}
    #RowTitle[muted="true"] {{ color: {p.muted}; }}
    #RowText {{ color: {p.muted}; font-size: 12px; }}
    #RowText[faint="true"] {{ color: {p.faint}; }}
    #Strong {{ color: {p.ink}; font-size: 12px; font-weight: 600; }}
    #Mono, #RowMono {{ font-family: {mono_font}; font-size: 12px; color: {p.text}; }}
    #RowMono {{ color: {p.muted}; }}
    #Tile {{ background: {p.sunken}; border-radius: 10px; }}
    #Avatar {{ border-radius: 16px; color: #ffffff; font-weight: 600; font-size: 13px; }}
    #AgentLogo {{ background: {p.primary}; color: {p.primary_text}; border-radius: 10px; font-size: 15px; font-weight: 600; }}
    #AgentLogo[installed="false"] {{ background: {p.sunken}; color: {p.faint}; }}
    #Chip {{ border-radius: 11px; }}
    #ChipText {{ font-size: 11px; font-weight: 500; }}
{chips}
    #CopyField {{ background: {p.sunken}; border: 1px solid {p.border}; border-radius: 8px; }}
    #KeyCap {{
        background: {p.surface}; border: 1px solid {p.border}; border-radius: 5px; padding: 2px 5px;
        font-family: {mono_font}; font-size: 11px; color: {p.text};
    }}
    #Illustration {{ background: {p.sunken}; border-radius: 8px; }}
    #MockButton {{ background: {p.surface}; border: 1.5px solid {p.ink}; border-radius: 6px; padding: 3px 10px;
                   font-size: 12px; font-weight: 600; color: {p.ink}; }}
    #StepNumber {{ background: {p.primary}; color: {p.primary_text}; border-radius: 11px; font-size: 12px; font-weight: 600; }}
    #Detail {{ background: {p.sunken}; border-radius: 10px; }}
    #DetailLabel {{ color: {p.faint}; font-size: 11px; font-weight: 500; }}
    #SummaryTitle {{ color: {p.ink}; font-size: 15px; font-weight: 600; }}
    #HeroTitle {{ font-size: 26px; font-weight: 600; color: {p.ink}; }}
    #ConfirmBox {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; }}
    QPlainTextEdit#Log {{
        background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px;
        font-family: {mono_font}; font-size: 12px; color: {p.muted}; padding: 8px;
    }}

    /* ---- pie ---- */
    #BottomBar {{ background: {p.background}; border-top: 1px solid {p.border}; }}
    #StatusText {{ color: {p.muted}; font-size: 13px; }}

    /* ---- controles ---- */
    QPushButton {{
        background: {p.surface}; color: {p.ink}; border: 1px solid {p.border};
        border-radius: {RADIUS_SMALL}px; padding: 8px 16px; font-size: 13px; font-weight: 600;
    }}
    QPushButton:hover {{ border-color: {p.faint}; }}
    QPushButton:disabled {{ color: {p.faint}; border-color: {p.border_soft}; }}
    QPushButton#Primary {{ background: {p.primary}; color: {p.primary_text}; border: 1px solid {p.primary}; }}
    QPushButton#Primary:hover {{ background: {p.primary_hover}; border-color: {p.primary_hover}; }}
    QPushButton#Primary:disabled {{ background: {p.toggle_off}; border-color: {p.toggle_off}; color: {p.surface}; }}
    QPushButton#Ghost {{ background: transparent; border-color: transparent; color: {p.muted}; }}
    QPushButton#Ghost:hover {{ color: {p.ink}; background: {p.sunken}; }}
    QPushButton#Subtle {{ background: {p.sunken}; border-color: {p.sunken}; color: {p.ink}; }}
    QPushButton#IconButton {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 6px; padding: 5px; }}
    QPushButton#IconButton:hover {{ border-color: {p.faint}; }}
    QPushButton#Chevron {{ background: transparent; border: none; padding: 4px; border-radius: 6px; }}
    QPushButton#Chevron:hover {{ background: {p.sunken}; }}
    QPushButton#Small {{ padding: 6px 12px; }}
    #Segmented {{ background: {p.sunken}; border-radius: 10px; }}
    QPushButton#Segment {{
        background: transparent; border: none; border-radius: 8px; color: {p.muted}; font-weight: 500; padding: 8px 18px;
    }}
    QPushButton#Segment:checked {{ background: {p.surface}; color: {p.ink}; font-weight: 600; }}
    QPushButton#Segment:disabled {{ color: {p.faint}; }}
    QRadioButton {{ spacing: 8px; font-size: 12px; color: {p.muted}; }}
    QRadioButton:checked {{ color: {p.ink}; font-weight: 500; }}
    QRadioButton::indicator {{
        width: 12px; height: 12px; border-radius: 7px; border: 1px solid {p.toggle_off}; background: {p.surface};
    }}
    QRadioButton::indicator:checked {{ width: 8px; height: 8px; border-radius: 7px; border: 3px solid {p.primary}; }}
    QScrollArea {{ border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {p.toggle_off}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {p.faint}; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
        background: none; border: none; height: 0;
    }}
    """
