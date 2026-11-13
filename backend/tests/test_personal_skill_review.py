"""Personal package notices reflect the same resources as platform packages."""
import io
import tarfile
import zipfile

from app.personal_skills import package_review
from app.site_skill_packages import review


def test_personal_scripts_have_execution_notices_without_per_file_warning_spam():
    files = {"SKILL.md": b"Read [guide](missing.md).", "scripts/task.py": b"print('ok')",
             "scripts/task.sh": b"echo ok", "scripts/task.mjs": b"console.log('ok')",
             ".gitignore": b"cache/", "assets/image.png": b"\xff\x00"}
    result = package_review(files)
    assert result["can_enable"] and result["has_limitations"]
    assert result["issues"] == []
    entries = {entry["path"]: entry for entry in result["manifest"]}
    for path in ("scripts/task.py", "scripts/task.sh", "scripts/task.mjs"):
        assert "按任务需要运行" in entries[path]["personal_notice"]
        assert not entries[path]["readable"]
    assert "随包保留" in entries[".gitignore"]["personal_notice"]
    assert "仅用于查看" in entries["assets/image.png"]["personal_notice"]
    assert review(files)["can_enable"]


def test_windows_script_limitation_is_explicit_and_text_only_needs_no_acknowledgement():
    result = package_review({"SKILL.md": b"Instructions", "scripts/task.ps1": b"Write-Output ok"})
    assert result["can_enable"] and not result["has_limitations"]
    assert result["issues"] == []
    entry = next(item for item in result["manifest"] if item["path"] == "scripts/task.ps1")
    assert entry["kind"] == "text" and "不能在 Linux 云电脑执行" in entry["personal_notice"]
    assert package_review({"SKILL.md": b"Instructions", "guide.md": b"Guide"})["has_limitations"] is False


def test_images_and_example_archives_are_resources():
    def zipped(files):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for path, content in files.items():
                archive.writestr(path, content)
        return output.getvalue()

    def tarred(files):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for path, content in files.items():
                info = tarfile.TarInfo(path); info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    result = package_review({
        "SKILL.md": b"Instructions", "assets/image.png": b"\x89PNG\r\n\x1a\n",
        "samples/input.zip": zipped({"input.csv": b"a,b\n1,2\n"}),
        "samples/output.tar.gz": tarred({"output.txt": b"Example"}),
    })
    assert result["can_enable"] and not result["has_limitations"] and result["issues"] == []
    entries = {entry["path"]: entry for entry in result["manifest"]}
    assert entries["assets/image.png"]["kind"] == "image" and entries["assets/image.png"]["resource"]
    assert entries["samples/input.zip"]["archive_entries"] == [{"path": "input.csv", "size": 8}]
    assert "不会自动解压" in entries["samples/output.tar.gz"]["personal_notice"]


def test_invalid_nested_tar_blocks_personal_save():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("link.txt")
        info.type, info.linkname = tarfile.SYMTYPE, "../../secret"
        archive.addfile(info)
    result = package_review({"SKILL.md": b"Instructions", "samples/link.tar.gz": output.getvalue()})
    assert not result["can_enable"]
    assert "TAR 不允许链接" in str(result["issues"])
