"""Opt-in paid smoke test: real sandbox, isolated local accounts and object store."""
import io
import hashlib
import json
import os
import shlex
from decimal import Decimal
import zipfile

import pytest
from sqlalchemy import select

from app import cloud_computer as cloud
from app.cloud_computer_adapter import AlibabaSandbox
from app.models import CloudComputerSettings, CreditAccount, UsageEvent
from app.site_auth import protect, reveal
from test_chat_attachments import PNG
from test_cloud_computer import setup, submit
from test_resources import resource_store, upload


@pytest.mark.skipif(os.environ.get("RAG_CLOUD_LIVE_ACCEPT") != "1" or not os.environ.get("RAG_CLOUD_CHECK_KEY"),
    reason="paid sandbox test requires explicit opt-in and a test key")
def test_real_cloud_resource_versions_and_lifetime_billing(accounts, monkeypatch, resource_store, tmp_path):
    client, db, conversation, access, _, factory = setup(accounts, monkeypatch, rate="1")
    settings = db.get(CloudComputerSettings, 1)
    settings.config = {**settings.config, "template": os.environ.get("RAG_CLOUD_TEST_TEMPLATE", "eidpiyb7xazavqt0n4go"),
        "region": "cn-hangzhou", "idle_seconds": 60, "max_seconds": 180}
    settings.encrypted_api_key = protect(os.environ["RAG_CLOUD_CHECK_KEY"])
    db.commit()
    real_provider = AlibabaSandbox(settings.config, reveal(settings.encrypted_api_key))
    monkeypatch.setattr(cloud, "adapter", lambda config, encrypted: real_provider)
    photo = upload(client, "photo.png", PNG)
    balance = db.get(CreditAccount, access.workspace_id).balance
    report = {"model_called": False, "object_store": "isolated in-memory fixture", "commands": []}
    computer_id = None
    try:
        def run(command, inputs=None):
            nonlocal computer_id
            saved = submit(db, access, conversation, command=command, inputs=inputs)
            computer_id = saved.session_id
            cloud.run(saved.id, factory)
            db.expire_all()
            saved = db.get(cloud.Command, saved.id)
            report["commands"].append({"id": str(saved.id), "status": saved.status})
            assert saved.status == "completed", saved.result
            return saved

        run("python3 -m pip install --disable-pip-version-check --no-cache-dir python-docx==1.1.2 pillow==12.0.0 reportlab==4.4.5")
        script = '''from docx import Document
from reportlab.pdfgen import canvas
d=Document();d.add_paragraph('Synthetic resume');d.add_picture(PHOTO_PATH);d.save('resume.docx')
pdf=canvas.Canvas('summary.pdf');pdf.drawString(40,760,'Synthetic cloud task');pdf.save()
open('summary.html','w').write('<html><body>Synthetic cloud task</body></html>')
'''.replace("PHOTO_PATH", repr(photo["path"]))
        created = run("python3 -c "+shlex.quote(script), [{"id": photo["id"], "version": 1}])
        document = next(item for item in created.result["generated_files"] if item["path"] == "resume.docx")
        first = client.get(f"/resources/{document['id']}/download?version=1")
        assert first.status_code == 200
        with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
            assert "word/media/image1.png" in archive.namelist()
        html = next(item for item in created.result["generated_files"] if item["path"] == "summary.html")
        pdf = next(item for item in created.result["generated_files"] if item["path"] == "summary.pdf")
        artifacts = {"resume-v1.docx": first.content}
        for item in (html, pdf):
            response = client.get(f"/resources/{item['id']}/download?version={item['version']}")
            assert response.status_code == 200 and response.content
            artifacts[item["path"]] = response.content
        updated = run("python3 -c "+shlex.quote("from docx import Document; d=Document('resume.docx'); d.add_paragraph('Second command'); d.save('resume.docx')"))
        document2 = next(item for item in updated.result["generated_files"] if item["path"] == "resume.docx")
        assert document2["id"] == document["id"] and document2["version"] == 2
        second = client.get(f"/resources/{document['id']}/download?version=2")
        assert second.status_code == 200 and first.content != second.content
        artifacts["resume-v2.docx"] = second.content
        assert client.get(f"/resources/{document['id']}/download?version=1").content == first.content
        assert submit(db, access, conversation, command=updated.payload["command"], request=updated.request_id).id == updated.id
        assert client.post(f"/conversations/{conversation.id}/cloud-computer/end", json={}).status_code == 200
        cloud.maintenance(factory)
        db.expire_all()
        row = db.get(cloud.Computer, computer_id)
        assert row.ended_at and row.held_credits == 0
        assert db.get(CreditAccount, access.workspace_id).balance == balance-row.charged_credits
        billed = sum(event.credits for event in db.scalars(select(UsageEvent).where(UsageEvent.capability == "cloud_computer")))
        assert billed == row.charged_credits > Decimal(0)
        report.update({"started_at": row.started_at.isoformat(), "ended_at": row.ended_at.isoformat(),
            "seconds": row.billed_seconds, "test_credits": str(billed), "final_held": str(row.held_credits),
            "resource_versions": [1, 2], "destroy_confirmed": True})
        for name, content in artifacts.items():
            (tmp_path/name).write_bytes(content)
        report["artifact_sha256"] = {name: hashlib.sha256(content).hexdigest() for name, content in artifacts.items()}
    finally:
        db.rollback()
        for row in db.scalars(select(cloud.Computer)):
            provider = cloud.adapter(row.snapshot, row.encrypted_api_key)
            for identifier, _ in provider.find(row.id):
                assert provider.kill(identifier), "test sandbox destruction not confirmed"
        (tmp_path/"cloud-live-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("cloud_live_report", json.dumps(report))
