from uuid import uuid4

from sqlalchemy import MetaData, Table


def legacy_tables(bind, *names):
    metadata = MetaData()
    return {name: Table(name, metadata, autoload_with=bind, resolve_fks=False) for name in names}


def seed_workspace(connection):
    tables = legacy_tables(connection, "users", "workspaces", "memberships")
    user_id, workspace_id = uuid4(), uuid4()
    connection.execute(tables["users"].insert().values(
        id=user_id, name="Migration test", email=f"migration-{user_id.hex}@example.com"))
    connection.execute(tables["workspaces"].insert().values(id=workspace_id, name="Migration test"))
    connection.execute(tables["memberships"].insert().values(
        workspace_id=workspace_id, user_id=user_id, role="admin"))
    return user_id, workspace_id
