"""Redacción de secretos y PII antes de que un resultado llegue a Claude o al visor.

- Tokens y credenciales: siempre.
- Emails: se conservan para investigar; el visor controla su ocultación visual por consulta.
- Teléfonos: se enmascaran.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"

_TOKEN_PATTERNS = [
    # clave=valor o "clave": "valor" con nombres típicos de credenciales
    re.compile(
        r"""(?ix)
        (["']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|apikey|secret|client[_-]?secret|
        password|passwd|authorization|x-api-key|session[_-]?token)["']?\s*[:=]\s*["']?)
        ([^"'\s&,;}]+)
        """
    ),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
]
_STANDALONE_TOKENS = [
    re.compile(r"\bya29\.[A-Za-z0-9._-]{10,}"),                                  # Google OAuth access token
    re.compile(r"\b1//[A-Za-z0-9._-]{20,}"),                                     # Google refresh token
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"),                # Stripe
    re.compile(r"\bEAA[A-Za-z0-9]{30,}"),                                        # Meta
]
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?![\w.])")


def _mask_phone(match: re.Match) -> str:
    raw = match.group(0)
    digits = re.sub(r"\D", "", raw)
    # Evita falsos positivos: fechas, timestamps y IDs largos.
    if not 10 <= len(digits) <= 15 or re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", raw) or raw.isdigit():
        return raw
    return f"***{digits[-2:]}"


class Redactor:
    def __init__(self, *, mask_emails: bool = False):
        self.mask_emails = mask_emails

    def text(self, value: str) -> str:
        for pattern in _TOKEN_PATTERNS:
            value = pattern.sub(lambda m: m.group(1) + MASK, value)
        for pattern in _STANDALONE_TOKENS:
            value = pattern.sub(MASK, value)
        if self.mask_emails:
            value = EMAIL.sub(lambda m: m.group(0)[0] + "***@" + m.group(0).split("@", 1)[1], value)
        return PHONE.sub(_mask_phone, value)

    def value(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            if key and _SENSITIVE_KEY.search(key):
                return MASK
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, key) for v in value]
        return value


_SENSITIVE_KEY = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|credential)")
