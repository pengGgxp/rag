"""Basic archive safety and permissive Markdown references."""
import io
import stat
import tarfile
import zipfile

import pytest
from fastapi import HTTPException
from app.site_skill_packages import review
from app.skill_files import archive_entries, unzip


def zipped(files, root=""):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(root + path, content)
    return result.getvalue()


def test_missing_and_malformed_markdown_links_do_not_block_use():
    result = review({"SKILL.md": b"Read [later](missing.md), [outside](../later.md), [bad](http://[)."})
    assert result["can_enable"] and result["issues"] == []


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../file", "a//file", "C:/file"])
def test_unpack_rejects_unsafe_paths(path):
    with pytest.raises(HTTPException):
        unzip(zipped({"SKILL.md": b"ok", path: b"bad"}))


def test_zip_rejects_special_entries_and_original_nul_paths():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", b"ok")
        link = zipfile.ZipInfo("link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../secret")
    with pytest.raises(HTTPException):
        unzip(output.getvalue())
    for before, after in [(b"nul-file.md", b"nul\x00file.md"), (b"refs/file.md", b"refs\\file.md")]:
        payload = zipped({"SKILL.md": b"ok", before.decode(): b"bad"}).replace(before, after)
        with pytest.raises(HTTPException):
            unzip(payload)


def test_example_archives_can_be_inspected_without_extraction():
    assert archive_entries("example.zip", zipped({"nested/data.csv": b"1 2"})) == [{"path": "nested/data.csv", "size": 3}]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("link")
        info.type, info.linkname = tarfile.SYMTYPE, "../../secret"
        archive.addfile(info)
    with pytest.raises(HTTPException):
        archive_entries("example.tar.gz", output.getvalue())
