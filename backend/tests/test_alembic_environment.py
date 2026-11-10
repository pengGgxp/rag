from pathlib import Path


def test_migration_database_url_takes_precedence():
    source = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )
    assert 'os.environ.get("MIGRATION_DATABASE_URL")' in source
    assert "or get_settings().database_url" in source
