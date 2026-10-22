"""El pack de prueba (tests/fixtures/pack-acme), para los tests que necesitan fuentes, Slack y skills."""
from pathlib import Path

from casebook import config

PACK = Path(__file__).resolve().parent / "fixtures" / "pack-acme" / "pack.toml"


def acme(path: Path | None = None) -> config.Config:
    """config.load con el pack de prueba como base; sin `path`, sin config.toml del usuario (como un Config() a
    mano: no se recarga ni se guarda)."""
    cfg = config.load(path or Path("/nonexistent/config.toml"), PACK)
    if path is None:
        cfg.source = None
    return cfg


def no_pack() -> None:
    """Deja la app sin pack activo (el PACKS_DIR del test)."""
    (config.PACKS_DIR / "active").unlink(missing_ok=True)
