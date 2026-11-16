from uuid import UUID, uuid4

from app.models import ChatMessage


def answer_identity(client, db):
    conversation = client.post("/conversations", json={})
    assert conversation.status_code == 201, conversation.text
    request = uuid4()
    row = ChatMessage(conversation_id=UUID(conversation.json()["id"]), request_id=request,
        sequence=1, role="assistant", content="")
    db.add(row)
    db.commit()
    return row.id, request
