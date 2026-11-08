import pytest

from casebook import __version__, cli


@pytest.mark.parametrize("flag", ["--version", "-v"])
def test_version_flag_prints_installed_version(flag, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([flag])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"casebook {__version__}"


def test_pack_export_keeps_its_own_version_option():
    # `casebook pack export --version X` is the pack's version, not the CLI flag.
    with pytest.raises(SystemExit) as exc:
        cli.main(["pack", "--version"])
    assert exc.value.code != 0  # needs a value: still parsed by the pack subcommand
