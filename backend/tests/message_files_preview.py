"""隔离页面验收：真实鉴权/Agent/文件API，本地命令、对象存储与合成模型。

运行 python tests/message_files_preview.py；前端 API 指向 :8136。
仅用于本地验收，不挂载到产品服务，不访问真实供应商。
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace


def create_files(root):
    from PIL import Image
    from pypdf import PdfWriter
    from zipfile import ZipFile
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 480), "#2878b5").save(root / "当前图片.png")
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(root / "检查.pdf")
    (root / "说明.txt").write_text("这是命令保存的说明文件。", encoding="utf-8")
    (root / "生成脚本.py").write_text('print("验收脚本")\n', encoding="utf-8")
    for directory in ("目录甲", "目录乙"):
        (root / directory).mkdir(exist_ok=True)
        (root / directory / "同名.txt").write_text(directory, encoding="utf-8")
    for number in range(14):
        (root / f"过程文件-{number + 1:02d}-这是一段需要完整辨认的中文长文件名称.txt").write_text(str(number), encoding="utf-8")
    # 最小可打开 OOXML 演示文稿；不声称在线渲染已支持 PPT。
    with ZipFile(root / "用户需要的报告.pptx", "w") as archive:
        archive.writestr("[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>')
        archive.writestr("_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>')
        archive.writestr("ppt/presentation.xml",
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sldIdLst/>'
            '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>')


if len(sys.argv) > 1 and sys.argv[1] == "--create":
    create_files(sys.argv[2])
    sys.exit(0)

import chat_attachment_preview as fixture
from app import cloud_computer as cloud, conversation_jobs, deployment, resource_previews
from app import shared_resources, storage_downloads, tasks, workspace_files
from app.config import get_settings
from app.llm import ToolCall, ToolResponse, LLMUsage
from app.llm_providers import _encrypt_api_key
from app.models import ModelConnection, CloudComputerSettings
from fastapi.responses import Response
from PIL import Image
from test_cloud_computer_mount import MemoryMount, configure_mount
from cloud_command_fixtures import LocalFileCloud
from test_resources import access_for

os.environ["FRONTEND_URL"] = "http://127.0.0.1:3136"
get_settings.cache_clear()


if __name__ == "__main__":
    for middleware in fixture.main.app.user_middleware:
        if middleware.cls.__name__ == "CORSMiddleware":
            middleware.kwargs["allow_origins"].append(os.environ["FRONTEND_URL"])
    fixture.Base.metadata.create_all(fixture.engine)
    storage = MemoryMount()
    workspace_files.upload_fileobj = storage.put
    storage_downloads.download_range_fileobj = lambda key, start, end: io.BytesIO(storage.objects[key][start:end + 1])
    resource_previews.current_storage = lambda *_: storage
    shared_resources.backend = lambda *_: storage
    resource_previews.dispatch = lambda identifier: Thread(target=resource_previews.run,
        args=(identifier, fixture.SessionLocal), daemon=True).start()
    deployment.is_active = lambda: False
    executor = ThreadPoolExecutor(max_workers=1)
    conversation_jobs.dispatch = lambda identifier: executor.submit(
        conversation_jobs.run, identifier, factory=fixture.SessionLocal)
    tasks.name_conversation.delay = lambda *_: None

    @asynccontextmanager
    async def lifespan(_):
        yield
    fixture.main.app.router.lifespan_context = lifespan

    with fixture.SessionLocal() as db:
        http = fixture.TestClient(fixture.main.app)
        user = fixture.authenticate(http, db)
        user.email_verified_at = fixture.auth.now()
        fixture.configured_model(db, fixture.auth.LEGACY_WORKSPACE_ID)
        for model in db.query(fixture.SiteModelProvider):
            model.name = "文件验收模型"
            model.supports_images = True
            model.context_capacity = 128000
            model.max_output_tokens = 4096
        for connection in db.query(ModelConnection):
            connection.encrypted_api_key = _encrypt_api_key("synthetic-only")
        db.add(CloudComputerSettings(id=1, version=1, enabled=True,
            config=cloud.Configuration(region="cn-hangzhou", template="test", specification="test",
                credits_per_minute=0, oss_role_arn="acs:ram::123:role/test").model_dump(mode="json"),
            encrypted_api_key="test-only"))
        db.commit()
        configure_mount(db, fixture.FixturePatch(), storage)
        actor = access_for(http, db)
        cookie = http.cookies.get(fixture.auth.COOKIE)
    root = Path(tempfile.mkdtemp(prefix="rag-issue106-files-"))
    prefix = shared_resources.prefix(actor.workspace_id)

    class LocalCommandCloud(LocalFileCloud):
        def start_file(self, sandbox, identifier, operation):
            return super().start_file(sandbox, identifier, {**operation,
                "path": operation["path"].removeprefix("/home/user/workspace/")})

        def start(self, sandbox, identifier, command):
            # 真正运行生成文件的子进程；仅此专用验收命令，绝不执行模型任意 shell。
            assert command == "issue106-create-files", command
            self.starts += 1
            process = subprocess.run([sys.executable, __file__, "--create", str(self.root)],
                capture_output=True, timeout=30, text=True)
            self.result = {"exit_code": process.returncode, "stdout": "文件已保存", "stderr": process.stderr}
            for target in self.root.rglob("*"):
                if target.is_file():
                    storage.objects[prefix + target.relative_to(self.root).as_posix()] = target.read_bytes()
            return 42

        def read_file(self, sandbox, path, max_bytes, check_active):
            check_active()
            return (self.root / path.removeprefix("/home/user/workspace/")).read_bytes()

    local_cloud = LocalCommandCloud(root, storage, prefix)
    cloud.adapter = lambda *_: local_cloud

    class Provider(fixture.Provider):
        def stream_with_tools(self, messages, tools, **kwargs):
            observations = [item for item in messages if item["role"] == "tool"]
            if not observations:
                result = ToolResponse(None, [ToolCall("make", "run_command", {"command": "issue106-create-files"})])
            elif observations[-1]["name"] == "run_command":
                assert json.loads(observations[-1]["content"])["status"] == "completed"
                paths = sorted(key[len(prefix):] for key in storage.objects if key.startswith(prefix))
                # 链接读取均经实际工具，不预先种植 ChatMessage/tool_events。
                result = ToolResponse("文件已保存，正在核对链接。", [
                    ToolCall(f"file-{index}", "get_workspace_file_link", {"path": path})
                    for index, path in enumerate(paths)])
            elif observations[-1]["name"] == "get_workspace_file_link":
                result = ToolResponse(None, [ToolCall("read", "read_image", {"path": "当前图片.png"})])
            elif observations[-1]["name"] == "read_image":
                assert not json.loads(observations[-1]["content"]).get("error")
                result = ToolResponse(None, [ToolCall("delete", "delete_file",
                    {"path": "过程文件-14-这是一段需要完整辨认的中文长文件名称.txt"})])
            else:
                file = next(json.loads(item["content"]) for item in observations
                    if item["name"] == "get_workspace_file_link" and "用户需要的报告" in item["content"])
                result = ToolResponse(f"请查看 [用户需要的报告.pptx]({file['url']})。"
                    "已保存文件并查看图片；演示文稿的版式质量尚未验收。", [])
            result = replace(result, usage=LLMUsage(100, 20, 120))
            self.last_usage = result.usage
            if result.content:
                yield "delta", {"text": result.content}
            yield "complete", result

    fixture.conversations.OpenAICompatibleChatProvider = lambda **_: Provider()

    @fixture.main.app.get("/__fixture/session")
    def session_fixture():
        return {"cookie": cookie}

    @fixture.main.app.post("/__fixture/overwrite-image")
    def overwrite_image():
        stream = io.BytesIO()
        Image.new("RGB", (640, 480), "#e04030").save(stream, "PNG")
        storage.objects[prefix + "当前图片.png"] = stream.getvalue()
        (root / "当前图片.png").write_bytes(stream.getvalue())
        return {"updated": True}

    fixture.uvicorn.run(fixture.main.app, host="0.0.0.0", port=8136, log_level="warning")
