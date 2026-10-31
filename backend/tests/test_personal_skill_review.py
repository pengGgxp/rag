"""Personal package notices reflect supported runtimes without relaxing platform review."""
from app.personal_skills import package_review
from app.site_skill_packages import review


def test_personal_scripts_have_execution_notices_without_per_file_warning_spam():
    files = {"SKILL.md": b"Read [guide](missing.md).", "scripts/task.py": b"print('ok')",
             "scripts/task.sh": b"echo ok", "scripts/task.mjs": b"console.log('ok')",
             ".gitignore": b"cache/", "assets/image.png": b"\xff\x00"}
    result = package_review(files)
    assert result["can_enable"] and result["has_limitations"]
    assert len(result["issues"]) == 1 and "missing.md" in result["issues"][0]
    entries = {entry["path"]: entry for entry in result["manifest"]}
    for path in ("scripts/task.py", "scripts/task.sh", "scripts/task.mjs"):
        assert "按任务需要运行" in entries[path]["personal_notice"]
        assert not entries[path]["readable"]
    assert "随包保留" in entries[".gitignore"]["personal_notice"]
    assert "随包保留" in entries["assets/image.png"]["personal_notice"]
    assert "不会执行" not in str(result)
    assert not review(files)["can_enable"]


def test_windows_script_limitation_is_explicit_and_text_only_needs_no_acknowledgement():
    result = package_review({"SKILL.md": b"Instructions", "scripts/task.ps1": b"Write-Output ok"})
    assert result["can_enable"] and result["has_limitations"]
    assert len(result["issues"]) == 1 and "暂不支持 Windows" in result["issues"][0]
    assert package_review({"SKILL.md": b"Instructions", "guide.md": b"Guide"})["has_limitations"] is False
