import subprocess
import sysconfig
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from dnd_server.authoring.cli import main

STUB_MESSAGE = "генератор появится в B5.6"


def test_stub_prints_message_and_returns_failure_code(capsys):
    assert main() == 1

    assert STUB_MESSAGE in capsys.readouterr().out


def test_dnd_generate_console_script_points_to_stub():
    [script] = entry_points(group="console_scripts", name="dnd-generate")

    assert script.value == "dnd_server.authoring.cli:main"
    assert script.load() is main


@pytest.mark.parametrize("arguments", [[], ["--help"]])
def test_installed_dnd_generate_exits_with_code_1(arguments):
    # README: заглушка завершается с кодом 1, и `--help` этого не меняет.
    script = Path(sysconfig.get_path("scripts")) / "dnd-generate"

    result = subprocess.run(
        [str(script), *arguments], capture_output=True, text=True, encoding="utf-8", timeout=60
    )

    assert result.returncode == 1
    assert STUB_MESSAGE in result.stdout
