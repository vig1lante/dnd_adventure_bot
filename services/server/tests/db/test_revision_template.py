"""Шаблон будущих миграций (`migrations/script.py.mako`) пишет заготовку по-русски."""

from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def test_a_new_revision_gets_its_comment_and_docstrings_in_russian(tmp_path):
    config = Config(str(ALEMBIC_INI))
    # Ревизия пишется во временную папку: настоящие миграции в тесте не появляются.
    config.set_main_option("path_separator", "os")
    config.set_main_option("version_locations", str(tmp_path))

    command.revision(config, message="template check", head="base")

    [created] = tmp_path.glob("*.py")
    text = created.read_text(encoding="utf-8")
    compile(text, str(created), "exec")
    assert "# Идентификаторы ревизии для Alembic." in text
    assert '"""Обновление схемы."""' in text
    assert '"""Откат схемы."""' in text
    for english in ("revision identifiers", "Upgrade schema", "Downgrade schema"):
        assert english not in text
