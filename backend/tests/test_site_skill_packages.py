"""Administrator package review, request-pinned resources and archive boundaries."""
import base64
import io
import json
import stat
import tarfile
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


@pytest.mark.parametrize("root", ["", "report/", "projects/report/"])
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
                                  {"references/code.sh": b"echo no"}, {"references/empty.bin": b""},
                                  {"references/windows.ps1": b"Write-Output no"},
                                  {"references/program.exe": b"MZ"}])
def test_linux_scripts_images_and_binaries_can_enable(accounts, monkeypatch, extra):
    admin, _, _ = setup_site(accounts, monkeypatch)
    draft = import_package(admin, {"SKILL.md": MAIN, "references/guide.md": b"guide", **extra}).json()
    assert draft["package_review"]["can_enable"]
    assert len(draft["files"]) == 2
    assert admin.post("/site/admin/skills", json=write_body(draft, True)).status_code == 201


def tarred(files, *, symlink=False):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            info.size = 0 if symlink else len(content)
            if symlink:
                info.type, info.linkname = tarfile.SYMTYPE, "../../secret"
            archive.addfile(info, None if symlink else io.BytesIO(content))
    return result.getvalue()


def encrypted_zip(path="samples/secret.zip"):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr("secret.txt", b"secret")
    data = bytearray(result.getvalue())
    marker = b"PK\x01\x02"
    central = data.rfind(marker)
    assert central >= 0
    flags = int.from_bytes(data[central + 8:central + 10], "little") | 1
    data[central + 8:central + 10] = flags.to_bytes(2, "little")
    return {path: bytes(data)}


def test_example_archives_render_members_and_unsafe_archives_block_enable(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    draft = import_package(admin, {
        "SKILL.md": MAIN, "references/guide.md": b"Guide",
        "samples/input.zip": zipped({"samples/input.csv": b"a,b\n1,2\n"}),
        "samples/output.tar.gz": tarred({"samples/output.txt": b"Example"}),
    }).json()
    assert draft["package_review"]["can_enable"]
    entries = {item["path"]: item for item in draft["package_review"]["manifest"]}
    assert entries["samples/input.zip"]["archive_entries"] == [{"path": "samples/input.csv", "size": 8}]
    assert entries["samples/output.tar.gz"]["archive_entries"] == [{"path": "samples/output.txt", "size": 7}]
    assert admin.post("/site/admin/skills", json=write_body(draft, True)).status_code == 201
    unsafe = import_package(admin, {
        "SKILL.md": MAIN, "samples/link.tar.gz": tarred({"safe.txt": b""}, symlink=True)}).json()
    assert not unsafe["package_review"]["can_enable"]
    assert "TAR 不允许链接" in str(unsafe["package_review"]["issues"])
    assert admin.post("/site/admin/skills", json=write_body(unsafe, True)).status_code == 422


def test_example_archive_rejects_encrypted_and_special_members(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    encrypted = import_package(admin, {
        "SKILL.md": MAIN, "references/guide.md": b"Guide", **encrypted_zip()}).json()
    entry = next(item for item in encrypted["package_review"]["manifest"]
        if item["path"] == "samples/secret.zip")
    assert not encrypted["package_review"]["can_enable"] and entry["blocking"]
    assert "加密" in entry["archive_error"]

    special_zip = io.BytesIO()
    with zipfile.ZipFile(special_zip, "w") as archive:
        info = zipfile.ZipInfo("samples/device")
        info.external_attr = (stat.S_IFCHR | 0o600) << 16
        archive.writestr(info, b"")
    zip_draft = admin.post("/site/admin/skills/import", files={
        "file": ("special.zip", zipped({
            "SKILL.md": MAIN, "references/guide.md": b"Guide",
            "samples/device.zip": special_zip.getvalue()}))}).json()
    assert not zip_draft["package_review"]["can_enable"]
    assert any("特殊文件" in issue for issue in zip_draft["package_review"]["issues"])

    special_tar = io.BytesIO()
    with tarfile.open(fileobj=special_tar, mode="w:gz") as archive:
        for path, content in (("SKILL.md", MAIN), ("references/guide.md", b"Guide")):
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        info = tarfile.TarInfo("samples/fifo")
        info.type = tarfile.FIFOTYPE
        archive.addfile(info)
    tar_draft = admin.post("/site/admin/skills/import", files={
        "file": ("special.zip", zipped({
            "SKILL.md": MAIN, "references/guide.md": b"Guide",
            "samples/fifo.tar.gz": special_tar.getvalue()}))}).json()
    assert not tar_draft["package_review"]["can_enable"]
    assert any("TAR 不允许链接或特殊文件" in issue
        for issue in tar_draft["package_review"]["issues"])


def test_example_archive_expanded_size_limit(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    draft = import_package(admin, {
        "SKILL.md": MAIN, "references/guide.md": b"Guide",
        "samples/large.zip": zipped({
            "one.bin": b"\0" * (5 * 1024 * 1024),
            "two.bin": b"\0" * (5 * 1024 * 1024),
            "three.bin": b"\0" * (5 * 1024 * 1024),
            "four.bin": b"\0" * (5 * 1024 * 1024),
            "five.bin": b"\0" * (1024 * 1024)})}).json()
    entry = next(item for item in draft["package_review"]["manifest"]
        if item["path"] == "samples/large.zip")
    assert not draft["package_review"]["can_enable"] and entry["blocking"]
    assert "大小限制" in entry["archive_error"]


def test_invalid_personal_example_archive_cannot_be_saved(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    draft = admin.post("/agent/skills/import", files={"file": ("bad.zip", zipped({
        "SKILL.md": MAIN, "samples/link.tar.gz": tarred({"safe.txt": b""}, symlink=True)}))}).json()
    assert not draft["review"]["can_enable"]
    body = {key: draft[key] for key in ("name", "description", "markdown", "files")}
    assert admin.post("/agent/skills", json={**body, "enabled": False}).status_code == 422


def test_personal_missing_reference_can_save_enable_and_be_discovered(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    draft = import_package(admin, {"SKILL.md": MAIN}).json()
    body = {key: draft[key] for key in ("name", "description", "markdown", "files")}
    assert admin.post("/agent/skills", json={**body, "enabled": False}).status_code == 201
    enabled = admin.post("/agent/skills", json={**body, "enabled": True})
    assert enabled.status_code == 201, enabled.text
    from test_extensions import access_for
    _, db = accounts
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db))
    assert enabled.json()["id"] in runtime.snapshots
    assert "不存在" in runtime.execute("read_skill_resource", {"skill_id": enabled.json()["id"], "path": "references/guide.md"})["error"]


def test_platform_unsafe_archive_saves_as_disabled_draft(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    unsafe = import_package(admin, {
        "SKILL.md": MAIN, "samples/link.tar.gz": tarred({"safe.txt": b""}, symlink=True)}).json()
    assert admin.post("/site/admin/skills", json=write_body(unsafe)).status_code == 201
    assert admin.post("/site/admin/skills", json=write_body(unsafe, True)).status_code == 422


def test_tar_member_count_limit(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    members = {f"empty-{index}.txt": b"" for index in range(601)}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, content in members.items():
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    draft = import_package(admin, {
        "SKILL.md": MAIN, "references/guide.md": b"Guide",
        "samples/too-many.tar": output.getvalue()}).json()
    assert not draft["package_review"]["can_enable"]
    assert any("too-many.tar" in issue for issue in draft["package_review"]["issues"])


@pytest.mark.parametrize("path", ["../secret.md", "/absolute.md", "root/x:secret.md", "references/../x.md", "a//x.md"])
def test_archive_paths_rejected(accounts, monkeypatch, path):
    admin, _, _ = setup_site(accounts, monkeypatch)
    response = import_package(admin, {"SKILL.md": b"# Main", path: b"unsafe"})
    assert response.status_code == 422


def test_archive_requires_one_nested_skill_root(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    missing = import_package(admin, {"project/README.md": b"Not a Skill"})
    assert missing.status_code == 422
    assert "包含 SKILL.md" in missing.json()["detail"]
    ambiguous = import_package(admin, {
        "first/SKILL.md": MAIN, "second/SKILL.md": MAIN})
    assert ambiguous.status_code == 422
    assert "多个 Skill 根目录" in ambiguous.json()["detail"]


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
    assert missing["package_review"]["can_enable"]
    assert admin.post("/site/admin/skills", json=write_body(missing, True)).status_code == 201
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
    assert runtime.execute("read_skill_resource", {**args, "path": "references/guide.md"})["content"] == "A" * 12000
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


def test_nested_folder_import_discovers_single_skill_root(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    response = admin.post("/agent/skills/import-folder", files=[
        ("files", ("SKILL.md", b"Instructions")), ("files", ("guide.txt", b"Guide"))],
        data={"paths": ["project/skills/report/SKILL.md", "project/skills/report/guide.txt"]})
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["name"] == "report"
    assert [item["path"] for item in draft["files"]] == ["guide.txt"]


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
