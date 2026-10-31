import base64, io, os, shutil, subprocess, tarfile, tempfile, uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app=FastAPI(title="workspace-executor")
class RunRequest(BaseModel):
    workspace_id: str
    command: str = Field(min_length=1, max_length=2000)
    files: dict[str,str] = Field(default_factory=dict)
@app.post("/run")
def run(payload: RunRequest):
    volume = f"workspace-exec-{uuid.uuid4().hex}"
    runner_image = os.environ.get("WORKSPACE_RUNNER_IMAGE", "rag-workspace-executor:latest")
    def docker(args, *, input_bytes=None, timeout=30):
        return subprocess.run(["docker", *args], input=input_bytes, capture_output=True, timeout=timeout)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for path, encoded in payload.files.items():
            target = Path(path)
            if target.is_absolute() or ".." in target.parts or "\\" in path or len(encoded) > 280_000_000:
                raise HTTPException(422, "文件输入无效")
            data = base64.b64decode(encoded)
            info = tarfile.TarInfo(path)
            info.size = len(data); info.mode = 0o644
            bundle.addfile(info, io.BytesIO(data))
    archive.seek(0)
    try:
        created = docker(["volume", "create", volume], timeout=15)
        if created.returncode != 0:
            raise HTTPException(503, "隔离执行环境暂时不可用")
        populated = docker(["run", "--rm", "-i", "--mount", f"source={volume},target=/workspace", runner_image,
            "tar", "-x", "-C", "/workspace"], input_bytes=archive.getvalue(), timeout=30)
        if populated.returncode != 0:
            raise HTTPException(503, "隔离执行环境无法准备工作区")
        container_name = f"workspace-exec-run-{uuid.uuid4().hex}"
        command=["docker","run","--name",container_name,"--rm","--network=bridge","--cpus=1","--memory=512m","--pids-limit=64",
            "--read-only","--cap-drop=ALL","--security-opt=no-new-privileges",
            "--mount",f"source={volume},target=/workspace,readonly=false","-w","/workspace",runner_image,"sh","-lc",payload.command]
        changed={}
        def export_files():
            exported = docker(["run", "--rm", "-i", "--mount", f"source={volume},target=/workspace,readonly=true",
                runner_image, "tar", "-c", "-C", "/workspace", "."], timeout=30)
            if exported.returncode != 0:
                return
            with tarfile.open(fileobj=io.BytesIO(exported.stdout), mode="r:") as bundle:
                for item in bundle.getmembers():
                    if item.isfile() and item.size <= 5_000_000:
                        handle = bundle.extractfile(item)
                        if handle: changed[item.name.lstrip("./")]=base64.b64encode(handle.read()).decode()
        try:
            completed=subprocess.run(command,text=True,capture_output=True,timeout=60)
        except subprocess.TimeoutExpired:
            docker(["rm", "-f", container_name], timeout=15)
            export_files()
            return {"status":"timeout","error":"脚本执行超时，已停止", "files": changed}
        export_files()
        return {"status":"completed" if completed.returncode==0 else "failed","exit_code":completed.returncode,"stdout":completed.stdout[-12000:],"stderr":completed.stderr[-12000:],"files":changed}
    finally:
        docker(["volume", "rm", "-f", volume], timeout=15)
