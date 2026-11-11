from uuid import uuid4

from test_auth import library, register


def test_knowledge_base_can_be_edited_archived_and_restored(accounts):
    (owner, _, _), _db = accounts
    register(owner)
    knowledge_base_id = library(owner)

    updated = owner.patch(
        f"/knowledge-bases/{knowledge_base_id}",
        json={
            "name": "Updated docs",
            "description": "Maintained reference",
            "tags": [" Product ", "内部", "product"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Updated docs"
    assert updated.json()["tags"] == ["Product", "内部"]

    archived = owner.post(f"/knowledge-bases/{knowledge_base_id}/archive")
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"

    blocked_search = owner.post(
        f"/knowledge-bases/{knowledge_base_id}/search",
        params={"query": "anything"},
    )
    assert blocked_search.status_code == 409
    conversation = owner.post("/conversations", json={}).json()
    assert conversation["knowledge_bases"] == []
    assert all(item["id"] != str(knowledge_base_id)
        for item in owner.get("/conversations/knowledge-catalog").json()["knowledge_bases"])
    blocked_mention = owner.post(f"/conversations/{conversation['id']}/messages", json={
        "provider_id": str(uuid4()), "request_id": str(uuid4()), "query": "搜索归档资料",
        "knowledge_base_ids": [str(knowledge_base_id)],
    })
    assert blocked_mention.status_code == 409

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
