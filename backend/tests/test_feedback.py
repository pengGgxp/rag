from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.models import AnswerFeedback, ChatMessage, Conversation
from test_conversations import client, create, turn
from test_retrieval import add_library
from test_auth import accounts, library, register, share, switch


def answer(http, db):
    conversation = create(http, [add_library(db)])
    events, _ = turn(http, conversation["id"])
    return conversation["id"], events[-1][1]["id"]


def test_feedback_persists_updates_and_can_be_removed_idempotently(client):
    http, db, _ = client
    conversation_id, message_id = answer(http, db)
    first = http.post("/feedback", json={"message_id": message_id, "rating": "up"})
    assert first.status_code == 200, first.text
    assert first.json()["rating"] == "up"
    again = http.post("/feedback", json={"message_id": message_id, "rating": "up"})
    assert again.json()["id"] == first.json()["id"]
    down = http.post("/feedback", json={
        "message_id": message_id, "rating": "down",
        "categories": ["missing_source", "citation_error"], "comment": "  请补充来源  ",
    })
    assert down.json()["id"] == first.json()["id"]
    detail = http.get(f"/conversations/{conversation_id}").json()
    feedback = detail["messages"][-1]["feedback"]
    assert feedback["rating"] == "down"
    assert feedback["categories"] == ["missing_source", "citation_error"]
    assert feedback["comment"] == "请补充来源"
    row = db.scalar(select(AnswerFeedback))
    conversation = db.get(Conversation, UUID(conversation_id))
    assert row.user_id == conversation.owner_id
    assert row.workspace_id == conversation.workspace_id
    assert row.message_id == UUID(message_id)
    assert db.scalar(select(func.count()).select_from(AnswerFeedback)) == 1
    # Switching to positive feedback clears old categories and comments.
    positive = http.post("/feedback", json={"message_id": message_id, "rating": "up"}).json()
    assert positive["categories"] == [] and positive["comment"] is None
    assert http.delete(f"/feedback/{message_id}").status_code == 204
    assert http.delete(f"/feedback/{message_id}").status_code == 204
    assert http.get(f"/conversations/{conversation_id}").json()["messages"][-1]["feedback"] is None


@pytest.mark.parametrize("fields", [
    {"rating": "down"},
    {"rating": "down", "categories": ["other"]},
    {"rating": "down", "categories": ["answer_error", "answer_error"]},
    {"rating": "up", "categories": ["answer_error"]},
    {"rating": "up", "comment": "not accepted"},
    {"rating": "down", "categories": ["answer_error"], "comment": "x"*2001},
    {"rating": "unknown"},
    {"rating": "up", "workspace_id": str(uuid4())},
])
def test_invalid_feedback_is_not_written(client, fields):
    http, db, _ = client
    _, message_id = answer(http, db)
    response = http.post("/feedback", json={"message_id": message_id, **fields})
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(AnswerFeedback)) == 0


def test_feedback_rejects_missing_user_incomplete_and_deleted_messages(client):
    http, db, _ = client
    conversation_id, message_id = answer(http, db)
    user_message = db.scalar(select(ChatMessage).where(ChatMessage.role == "user"))
    for id in [str(uuid4()), str(user_message.id)]:
        assert http.post("/feedback", json={"message_id": id, "rating": "up"}).status_code == 404
    message = db.get(ChatMessage, UUID(message_id))
    for status in ["streaming", "failed", "cancelled", "interrupted"]:
        message.status = status
        db.commit()
        assert http.post("/feedback", json={"message_id": message_id, "rating": "up"}).status_code == 409
    message.status = "completed"
    db.commit()
    assert http.post("/feedback", json={"message_id": message_id, "rating": "up"}).status_code == 200
    assert http.delete(f"/conversations/{conversation_id}").status_code == 204
    assert http.post("/feedback", json={"message_id": message_id, "rating": "up"}).status_code == 404
    assert http.delete(f"/feedback/{message_id}").status_code == 404
    assert http.get(f"/conversations/{conversation_id}").status_code == 404


def test_feedback_remains_private_to_conversation_owner_and_honors_revocation(accounts):
    (owner, viewer, anonymous), db = accounts
    a = register(owner)
    b = register(viewer, "viewer@example.com", "Viewer")
    id = library(owner)
    share(owner, "viewer@example.com")
    switch(viewer, a["workspace_id"])
    conversation = viewer.post("/conversations", json={}).json()
    message = ChatMessage(
        conversation_id=UUID(conversation["id"]), sequence=1,
        role="assistant", content="Example answer", status="completed",
    )
    db.add(message)
    db.commit()
    message_id = str(message.id)
    payload = {"message_id": message_id, "rating": "down", "categories": ["answer_error"]}
    assert anonymous.post("/feedback", json=payload).status_code == 401
    assert owner.post("/feedback", json=payload).status_code == 404
    assert owner.delete(f"/feedback/{message_id}").status_code == 404
    assert viewer.post("/feedback", json=payload).status_code == 200
    assert db.scalar(select(func.count()).select_from(AnswerFeedback)) == 1
    assert owner.get(f"/conversations/{conversation['id']}").status_code == 404
    assert owner.put(f"/knowledge-bases/{id}/permissions", json={"restricted": True}).status_code == 200
    assert viewer.post("/feedback", json=payload).status_code == 200
    assert viewer.delete(f"/feedback/{message_id}").status_code == 204
    assert viewer.get(f"/conversations/{conversation['id']}").status_code == 200
    # A different active workspace never grants access even to one's own old messages.
    switch(viewer, b["workspace_id"])
    assert viewer.post("/feedback", json=payload).status_code == 404
    assert db.scalar(select(func.count()).select_from(AnswerFeedback)) == 0
