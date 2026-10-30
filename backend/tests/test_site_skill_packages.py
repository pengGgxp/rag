"""Administrator package review, request-pinned resources and archive boundaries."""
import base64
import io
import json
import stat
import zipfile
from contextlib import nullcontext
from uuid import UUID

import pytest

from app import conversations
from app.extensions import ExtensionRuntime
from app.llm import LLMUsage, ToolCall, ToolResponse
from app.models import SiteAuditEvent, SiteSkill
from sqlalchemy import select
from auth_helpers import StreamingTestProvider
from test_extensions import access_for
from test_auth import register
from test_shared_providers import setup_site, provider
from test_request_models import send

MAIN = b"---\nname: report-method\ndescription: Create reports\n---\nRead [guide](references/guide.md) before writing."


def zipped(files, root=""):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(root + path, content)
    return result.getvalue()


def import_package(admin, files=None, root=""):
    return admin.post("/site/admin/skills/import", files={"file": ("report.zip", zipped(files or {
        "SKILL.md": MAIN, "references/guide.md": b"Only use the heading Evidence."}, root))})


def write_body(draft, enabled=False):
    return {key: draft[key] for key in ("name", "description", "markdown", "files")} | {"enabled": enabled}


def test_package_import_review_require_site_administrator(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    (_, member, anonymous), _ = accounts
    register(member, "package-member@example.com")
    for client, status in ((member, 403), (anonymous, 401)):
        assert import_package(client).status_code == status
        assert client.post("/site/admin/skills/review", json={"markdown": "# Test", "files": []}).status_code == status


@pytest.mark.parametrize("root", ["", "report/"])
def test_import_package_preserves_review_and_atomic_resources(accounts, monkeypatch, root):
    admin, db, _ = setup_site(accounts, monkeypatch)
    response = import_package(admin, root=root)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["package_review"]["can_enable"] and not draft["enabled"]
    assert base64.b64decode(draft["files"][0]["content"]) == b"Only use the heading Evidence."
    assert admin.get("/site/admin/skills").json()["total"] == 0
    body = write_body(draft, True)
    result = admin.post("/site/admin/skills", json=body)
    assert result.status_code == 201, result.text
    item = result.json()
    assert item["files"] == body["files"] and item["review"]["can_enable"]
    resource = item["files"][0]
    changed = {**body, "files": [{**resource, "content": base64.b64encode(b"Changed guide").decode()}], "version": 1}
    assert admin.put(f"/site/admin/skills/{item['id']}", json=changed).status_code == 200
    conflict = admin.put(f"/site/admin/skills/{item['id']}", json=changed)
    assert conflict.status_code == 409
    detail = admin.get(f"/site/admin/skills/{item['id']}").json()
    assert base64.b64decode(detail["files"][0]["content"]) == b"Changed guide"
    events = db.scalars(select(SiteAuditEvent).where(SiteAuditEvent.target_type == "site_skill")).all()
    assert "files" in events[-1].details["fields"]
    assert "Changed guide" not in str([event.details for event in events])
    assert admin.request("DELETE", f"/site/admin/skills/{item['id']}", json={"version": 2}).status_code == 204


@pytest.mark.parametrize("extra", [{"scripts/run.py": b"print('no')"}, {"assets/image.png": b"\x89PNG\x00"},
                                  {"references/code.sh": b"echo no"}, {"references/empty.bin": b""}])
def test_unsupported_resources_retained_as_draft_and_cannot_enable(accounts, monkeypatch, extra):
    admin, _, _ = setup_site(accounts, monkeypatch)
    draft = import_package(admin, {"SKILL.md": MAIN, "references/guide.md": b"guide", **extra}).json()
    assert not draft["package_review"]["can_enable"]
    assert len(draft["files"]) == 2
    assert admin.post("/site/admin/skills", json=write_body(draft, True)).status_code == 422
    saved = admin.post("/site/admin/skills", json=write_body(draft)).json()
    assert len(saved["files"]) == 2
    saved["files"] = [file for file in saved["files"] if file["path"] == "references/guide.md"]
    updated = admin.put(f"/site/admin/skills/{saved['id']}", json={**write_body(saved, True), "version": 1})
    assert updated.status_code == 200 and updated.json()["enabled"]


@pytest.mark.parametrize("path", ["../secret.md", "/absolute.md", "root/x:secret.md", "references/../x.md", "a//x.md"])
def test_archive_paths_rejected(accounts, monkeypatch, path):
    admin, _, _ = setup_site(accounts, monkeypatch)
    response = import_package(admin, {"SKILL.md": b"# Main", path: b"unsafe"})
    assert response.status_code == 422


def test_archive_links_duplicates_missing_references_and_limits(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("SKILL.md", MAIN)
        link = zipfile.ZipInfo("references/link.md")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../secret")
    assert admin.post("/site/admin/skills/import", files={"file": ("bad.zip", data.getvalue())}).status_code == 422
    assert import_package(admin, {"SKILL.md": MAIN, "skill.md": MAIN}).status_code == 422
    assert import_package(admin, {"SKILL.md": MAIN, "references/big.md": b"x" * (5*1024*1024+1)}).status_code == 413
    missing = import_package(admin, {"SKILL.md": MAIN}).json()
    assert not missing["package_review"]["can_enable"]
    assert admin.post("/site/admin/skills", json=write_body(missing, True)).status_code == 422
    # File review uses exactly the same rules as activation.
    response = admin.post("/site/admin/skills/review", json={"markdown": missing["markdown"], "files": []})
    assert response.json()["issues"] == missing["package_review"]["issues"]
    duplicate = {"name": "test", "description": "test", "markdown": "body", "files": [
        {"path": "references/a.md", "content": "YQ=="}, {"path": "references/a.md", "content": "Yg=="}]}
    assert admin.post("/site/admin/skills", json=duplicate).status_code == 422
    backslash = zipped({"SKILL.md": b"# Main", "references/guide.md": b"safe"}).replace(
        b"references/guide.md", b"references\\guide.md")
    assert admin.post("/site/admin/skills/import", files={"file": ("bad.zip", backslash)}).status_code == 422
    nul = zipped({"SKILL.md": b"# Main", "nul-file.md": b"safe"}).replace(b"nul-file.md", b"nul\x00file.md")
    assert admin.post("/site/admin/skills/import", files={"file": ("bad.zip", nul)}).status_code == 422


def test_resource_snapshot_revocation_boundaries_and_paging(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    draft = import_package(admin, {"SKILL.md": MAIN, "references/guide.md": b"A" * 13000}).json()
    saved = admin.post("/site/admin/skills", json=write_body(draft, True)).json()
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db))
    args = {"skill_id": saved["id"]}
    assert runtime.execute("read_skill_resource", {**args, "path": "references/guide.md"}).get("error")
    assert runtime.execute("load_skill", {})["error"]
    loaded = runtime.execute("load_skill", args)
    assert loaded["resources"] == [{"path": "references/guide.md"}]
    assert "AAAA" not in str(loaded)
    assert runtime.execute("read_skill_resource", {**args, "path": "../private.md"}).get("error")
    row = db.get(SiteSkill, UUID(saved["id"]))
    row.files[0].content, row.version = b"NEW", 2
    db.commit()
    first = runtime.execute("read_skill_resource", {**args, "path": "references/guide.md"})
    assert first["content"] == "A" * 12000 and first["next_offset"] == 12000 and first["version"] == 1
    second = runtime.execute("read_skill_resource", {**args, "path": "references/guide.md", "offset": first["next_offset"]})
    assert second["content"] == "A" * 1000 and second["next_offset"] is None
    for offset in (-1, True, 15000):
        assert runtime.execute("read_skill_resource", {**args, "path": "references/guide.md", "offset": offset}).get("error")
    row.enabled = False; db.commit()
    assert runtime.execute("read_skill_resource", {**args, "path": "references/guide.md"}).get("error")


def test_agent_loads_package_resources_in_order_and_persists_safe_activity(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    model = provider(admin)
    draft = import_package(admin).json()
    item = admin.post("/site/admin/skills", json=write_body(draft, True)).json()
    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            self.last_usage = LLMUsage(prompt_tokens=20, completion_tokens=3)
            if messages[-1]["role"] == "user":
                assert "Only use the heading" not in str(messages)
                return ToolResponse(None, [ToolCall("method", "load_skill", {"skill_id": item["id"]})], self.last_usage)
            result = json.loads(messages[-1]["content"])
            if "instructions" in result:
                return ToolResponse(None, [ToolCall("reference", "read_skill_resource", {"skill_id": item["id"], "path": "references/guide.md"})], self.last_usage)
            assert result["content"] == "Only use the heading Evidence."
            return ToolResponse("## Evidence\nReport", [], self.last_usage)
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    chat = admin.post("/conversations", json={}).json()
    events, _ = send(admin, chat["id"], model["id"])
    final = events[-1].json()
    assert final["status"] == "completed", final
    assert [event["kind"] for event in final["tool_events"]] == ["skill", "skill"]
    assert final["tool_events"][1]["skill_version"] == 1
    assert "Only use" not in str(final["tool_events"])
    assert admin.get(f"/conversations/{chat['id']}").json()["messages"][-1]["tool_events"] == final["tool_events"]
