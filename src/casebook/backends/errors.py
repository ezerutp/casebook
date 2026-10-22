"""Errores esperados de los backends: se le muestran a Claude con su mensaje."""


class BackendError(RuntimeError):
    """Sesión vencida, túnel caído, error del sistema consultado, etc."""


class SessionExpired(BackendError):
    pass
