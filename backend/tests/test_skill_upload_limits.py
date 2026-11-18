"""Capacity boundaries and real HTTP roundtrips for 200 MiB Skill packages."""
import base64
import hashlib
import io
import tarfile
import zipfile
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import skill_files
from app.models import SiteSkill, WorkspaceFile
from app.site_skill_packages import decode_files
from app.site_skills import SkillFileWrite
from test_resources import resource_store
from test_shared_providers import setup_site
from test_skill_resources import FILES, zipped


def test_capacity_and_encoded_schema_share_200_mib():
    assert skill_files.MAX_UPLOAD_BYTES == skill_files.MAX_FILE_BYTES == skill_files.MAX_BUNDLE_BYTES == 200 * 1024**2
    assert SkillFileWrite.model_json_schema()["properties"]["content"]["maxLength"] == 4 * ((200 * 1024**2 + 2) // 3)
    assert skill_files.MAX_FILES == 200


def test_real_200_mib_bundle_boundary_and_one_extra_byte():
    data = b"\xff" * skill_files.MAX_FILE_BYTES
    files = {"SKILL.md": b"", "assets/data.bin": data}
    assert skill_files.validate_files(files) is files
    with pytest.raises(HTTPException, match="总大小") as error:
        skill_files.validate_files({**files, "SKILL.md": b"x"})
    assert error.value.status_code == 413
    with pytest.raises(HTTPException, match="单个 Skill") as error:
        skill_files.validate_files({"SKILL.md": b"", "assets/data.bin": data + b"x"})
    assert error.value.status_code == 413


def test_real_200_mib_zip_upload_boundary_and_one_extra_byte():
    files = {"SKILL.md": b"ok", "assets/data.bin": b""}
    overhead = len(zipped(files))
    files["assets/data.bin"] = b"\xff" * (skill_files.MAX_UPLOAD_BYTES - overhead)
    data = zipped(files)
    assert len(data) == skill_files.MAX_UPLOAD_BYTES
    assert skill_files.unzip(data)[0] == files
    with pytest.raises(HTTPException) as error:
        skill_files.unzip(data + b"x")
    assert error.value.status_code == 413


def test_decoded_limits_include_manifest_and_base64_padding(monkeypatch):
    monkeypatch.setattr(skill_files, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(skill_files, "MAX_BUNDLE_BYTES", 10)
    item = lambda data: SimpleNamespace(path="data.bin", content=base64.b64encode(data).decode())
    assert decode_files([item(b"12345678")], "ok")["data.bin"] == b"12345678"
    with pytest.raises(HTTPException) as error:
        decode_files([item(b"123456789")], "x")
    assert error.value.status_code == 413
    with pytest.raises(HTTPException) as error:
        decode_files([item(b"12345678")], "abc")
    assert error.value.status_code == 413
    with pytest.raises(HTTPException) as error:
        decode_files([item(b"x" * 20)], "ok")
    assert error.value.status_code == 413


@pytest.mark.parametrize("suffix", [".zip", ".tar.gz"])
def test_archive_expansion_limit_is_inclusive_and_preserves_file_limit(monkeypatch, suffix):
    files = {"SKILL.md": b"ok", "data.bin": b"\xff" * 8}
    if suffix == ".zip":
        data = zipped(files)
    else:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for path, content in files.items():
                info = tarfile.TarInfo(path)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        data = output.getvalue()
    monkeypatch.setattr(skill_files, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(skill_files, "MAX_BUNDLE_BYTES", 10)
    assert sum(entry["size"] for entry in skill_files.archive_entries("example" + suffix, data)) == 10
    if suffix == ".zip":
        assert skill_files.unzip(data)[0] == files
    monkeypatch.setattr(skill_files, "MAX_BUNDLE_BYTES", 9)
    with pytest.raises(HTTPException) as error:
        skill_files.archive_entries("example" + suffix, data)
    assert error.value.status_code == 413
    if suffix == ".zip":
        with pytest.raises(HTTPException) as error:
            skill_files.unzip(data)
        assert error.value.status_code == 413
    monkeypatch.setattr(skill_files, "MAX_BUNDLE_BYTES", 10)
    monkeypatch.setattr(skill_files, "MAX_FILE_BYTES", 7)
    with pytest.raises(HTTPException) as error:
        skill_files.archive_entries("example" + suffix, data)
    assert error.value.status_code == 413


@pytest.mark.parametrize("endpoint", ["/site/admin/skills/import", "/agent/skills/import"])
def test_http_upload_boundary_is_inclusive_and_leaves_no_partial_files(accounts, resource_store, monkeypatch, endpoint):
    client, db, _ = setup_site(accounts, monkeypatch)
    data = zipped(FILES)
    monkeypatch.setattr(skill_files, "MAX_UPLOAD_BYTES", len(data))
    accepted = client.post(endpoint, files={"file": ("sample.zip", data)})
    assert accepted.status_code == (200 if "/admin/" in endpoint else 201), accepted.text
    previous = dict(resource_store[0])
    rejected = client.post(endpoint, files={"file": ("sample.zip", data + b"x")})
    assert rejected.status_code == 413 and "200 MiB" in rejected.json()["detail"]
    assert resource_store[0] == previous
    assert db.scalar(select(SiteSkill)) is None


@pytest.mark.parametrize("extra,total", [(False, 10), (True, 10), (False, 9)])
def test_folder_boundaries_fail_before_writes(accounts, resource_store, monkeypatch, extra, total):
    client, db, _ = setup_site(accounts, monkeypatch)
    monkeypatch.setattr(skill_files, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(skill_files, "MAX_BUNDLE_BYTES", total)
    response = client.post("/agent/skills/import-folder", files=[
        ("files", ("SKILL.md", b"ok")), ("files", ("data.bin", b"x" * (9 if extra else 8)))],
        data={"paths": ["sample/SKILL.md", "sample/data.bin"]})
    if not extra and total == 10:
        assert response.status_code == 201, response.text
    else:
        assert response.status_code == 413 and "200 MiB" in response.json()["detail"]
        assert not resource_store[0] and db.scalar(select(WorkspaceFile)) is None


@pytest.fixture
def large_files():
    return {"SKILL.md": FILES["SKILL.md"], "assets/data.bin": b"\xff" * (26 * 1024**2)}


@pytest.mark.parametrize("folder", [False, True])
def test_package_above_old_upload_limit_roundtrips_in_workspace(accounts, resource_store, monkeypatch, large_files, folder):
    client, _, _ = setup_site(accounts, monkeypatch)
    if folder:
        response = client.post("/agent/skills/import-folder",
            files=[("files", (path, data)) for path, data in large_files.items()],
            data={"paths": [f"sample/{path}" for path in large_files]})
    else:
        data = zipped(large_files)
        assert len(data) > 25 * 1024**2
        response = client.post("/agent/skills/import", files={"file": ("sample.zip", data)})
    assert response.status_code == 201, response.text
    result = response.json()
    for item in result["items"]:
        expected = large_files[item["path"].removeprefix(result["directory"] + "/")]
        downloaded = client.get(item["download_url"])
        assert downloaded.status_code == 200
        assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(expected).digest()


def test_large_admin_import_review_save_download_and_install(accounts, resource_store, monkeypatch, large_files):
    client, db, _ = setup_site(accounts, monkeypatch)
    data = zipped(large_files)
    assert len(data) > 25 * 1024**2
    response = client.post("/site/admin/skills/import", files={"file": ("sample.zip", data)})
    assert response.status_code == 200, response.text[:500]
    preview = response.json()
    assert preview["package_review"]["can_enable"]
    payload = {key: value for key, value in preview.items() if key != "package_review"}
    checked = client.post("/site/admin/skills/review", json={"markdown": payload["markdown"], "files": payload["files"]})
    assert checked.status_code == 200 and checked.json()["can_enable"]
    payload["enabled"] = True
    response = client.post("/site/admin/skills", json=payload)
    assert response.status_code == 201, response.text[:500]
    identifier = response.json()["id"]
    row = db.get(SiteSkill, UUID(identifier))
    expected = large_files["assets/data.bin"]
    assert row.files[0].content == expected
    updated = client.put(f"/site/admin/skills/{identifier}", json={**payload, "version": 1})
    assert updated.status_code == 200, updated.text[:500]
    assert updated.json()["version"] == 2
    with zipfile.ZipFile(io.BytesIO(client.get(f"/agent/skills/platform/{identifier}/download").content)) as archive:
        assert hashlib.sha256(archive.read("assets/data.bin")).digest() == hashlib.sha256(expected).digest()
    installed = client.post(f"/agent/skills/platform/{identifier}/install")
    assert installed.status_code == 201, installed.text[:500]
    resource = next(item for item in installed.json()["items"] if item["path"].endswith("/assets/data.bin"))
    downloaded = client.get(resource["download_url"])
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(expected).digest()
