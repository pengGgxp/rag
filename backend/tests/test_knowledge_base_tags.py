from test_auth import library, register


def test_knowledge_base_tags_are_normalized_and_searchable(accounts):
    (owner, _, _), _db = accounts
    register(owner)
    knowledge_base_id = library(owner)
    updated = owner.patch(
        f"/knowledge-bases/{knowledge_base_id}",
        json={"name": "Docs", "tags": ["  Internal ", "internal", "产品"]},
    )
    assert updated.status_code == 200
    assert updated.json()["tags"] == ["Internal", "产品"]

    listed = owner.get("/knowledge-bases")
    assert listed.status_code == 200
    assert listed.json()[0]["tags"] == ["Internal", "产品"]


def test_knowledge_base_tags_reject_empty_or_too_many_values(accounts):
    (owner, _, _), _db = accounts
    register(owner)
    knowledge_base_id = library(owner)
    assert owner.patch(
        f"/knowledge-bases/{knowledge_base_id}",
        json={"name": "Docs", "tags": [""]},
    ).status_code == 422
    assert owner.patch(
        f"/knowledge-bases/{knowledge_base_id}",
        json={"name": "Docs", "tags": [str(index) for index in range(13)]},
    ).status_code == 422
