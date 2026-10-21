from test_auth import library, register


def test_knowledge_base_can_be_edited_archived_and_restored(accounts):
    (owner, _, _), _db = accounts
    register(owner)
    knowledge_base_id = library(owner)

    updated = owner.patch(
        f"/knowledge-bases/{knowledge_base_id}",
        json={"name": "Updated docs", "description": "Maintained reference"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Updated docs"

    archived = owner.post(f"/knowledge-bases/{knowledge_base_id}/archive")
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"

    blocked_search = owner.post(
        f"/knowledge-bases/{knowledge_base_id}/search",
        params={"query": "anything"},
    )
    assert blocked_search.status_code == 409
    blocked_conversation = owner.post(
        "/conversations",
        json={"knowledge_base_ids": [str(knowledge_base_id)]},
    )
    assert blocked_conversation.status_code == 409

    restored = owner.post(f"/knowledge-bases/{knowledge_base_id}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "draft"


def test_archive_and_restore_are_idempotent(accounts):
    (owner, _, _), _db = accounts
    register(owner)
    knowledge_base_id = library(owner)

    assert owner.post(f"/knowledge-bases/{knowledge_base_id}/archive").status_code == 200
    assert owner.post(f"/knowledge-bases/{knowledge_base_id}/archive").status_code == 200
    assert owner.post(f"/knowledge-bases/{knowledge_base_id}/restore").status_code == 200
    assert owner.post(f"/knowledge-bases/{knowledge_base_id}/restore").status_code == 200
