from pathlib import Path


def test_app_root_only_contains_entrypoints_and_orm():
    app_root = Path(__file__).resolve().parents[1] / "app"
    assert {path.name for path in app_root.glob("*.py")} == {
        "__init__.py",
        "executor_service.py",
        "main.py",
        "manage.py",
        "migrate_cos.py",
        "migrate_knowledge_originals.py",
        "migrate_shared_resources.py",
        "migrate_storage.py",
        "models.py",
        "preview_gateway.py",
        "storage_models.py",
        "tasks.py",
    }
