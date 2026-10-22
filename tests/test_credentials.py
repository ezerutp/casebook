import pytest

from casebook import credentials
from casebook.config import DbCredentials


@pytest.fixture
def creds_file(tmp_path):
    return tmp_path / "credentials"


def source(path):
    """Lo que trae el pack de Acme: el archivo que escribe el alias db-login."""
    return DbCredentials(kind="env-file", path=str(path), user_var="DB_USER", password_var="DB_PASSWORD")


def read(path):
    return credentials.read_env_file(path, {"DB_USER", "DB_PASSWORD", "DB_SSH_USER"})


@pytest.fixture
def fake_keyring(monkeypatch):
    store = {}
    monkeypatch.setattr(credentials.keyring, "get_password", lambda svc, name: store.get(name))
    monkeypatch.setattr(credentials.keyring, "set_password", lambda svc, name, value: store.__setitem__(name, value))
    return store


def test_reads_an_env_file(creds_file):
    # Como lo escribe un script típico de credenciales: líneas `export NOMBRE=valor`.
    creds_file.write_text("export DB_USER=usuario\nexport DB_PASSWORD=s3cr3t!x\nexport DB_SSH_USER=usuario.bastion\n")
    assert read(creds_file) == {
        "DB_USER": "usuario", "DB_PASSWORD": "s3cr3t!x", "DB_SSH_USER": "usuario.bastion"
    }
    assert credentials.get_db_login(source(creds_file)) == ("usuario", "s3cr3t!x")
    assert credentials.db_login_source(source(creds_file)) == str(creds_file)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('export DB_PASSWORD="con espacios y #"', "con espacios y #"),
        ("export DB_PASSWORD='simple'", "simple"),
        ("DB_PASSWORD=sin_export", "sin_export"),
        ('export DB_PASSWORD="sin cerrar', '"sin cerrar'),
    ],
)
def test_quoting_like_source(creds_file, line, expected):
    creds_file.write_text(f"export DB_USER=u\n{line}\n")
    assert read(creds_file)["DB_PASSWORD"] == expected


def test_ignores_other_lines_and_is_never_executed(creds_file, tmp_path):
    marker = tmp_path / "ejecutado"
    creds_file.write_text(f"touch {marker}\nexport OTRA=1\n# comentario\nexport DB_USER=u\nexport DB_PASSWORD=p\n")
    assert read(creds_file) == {"DB_USER": "u", "DB_PASSWORD": "p"}
    assert not marker.exists()


def test_creds_file_wins_over_keyring(creds_file, fake_keyring):
    credentials.set_db_login("viejo", "vieja")
    assert credentials.get_db_login(source(creds_file)) == ("viejo", "vieja")  # sin archivo: keyring
    assert credentials.db_login_source(source(creds_file)) == "keyring (casebook login db)"
    creds_file.write_text("export DB_USER=nuevo\nexport DB_PASSWORD=nueva\n")
    assert credentials.get_db_login(source(creds_file)) == ("nuevo", "nueva")


def test_nothing_configured(creds_file, fake_keyring):
    assert credentials.get_db_login(source(creds_file)) is None
    assert credentials.db_login_source(source(creds_file)) is None
    assert credentials.get_db_login() is None  # sin fuente: solo el keyring
