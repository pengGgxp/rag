import base64, io, os, subprocess, tarfile, uuid
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from .skill_files import safe_path, MAX_BUNDLE_BYTES

app=FastAPI(title="workspace-executor")
RUNNER_NETWORK = "rag-workspace-runners"
# Applied only to the dedicated runner bridge, never the application's network.
NETWORK_RULES = """
set -eu
iptables -N RAG-EXEC-EGRESS 2>/dev/null || true
for cidr in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 192.0.0.0/24 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
  iptables -C RAG-EXEC-EGRESS -d "$cidr" -j REJECT 2>/dev/null || iptables -A RAG-EXEC-EGRESS -d "$cidr" -j REJECT
done
iptables -C DOCKER-USER -i rag-exec0 -j RAG-EXEC-EGRESS 2>/dev/null || iptables -I DOCKER-USER 1 -i rag-exec0 -j RAG-EXEC-EGRESS
iptables -C INPUT -i rag-exec0 -j REJECT 2>/dev/null || iptables -I INPUT 1 -i rag-exec0 -j REJECT
"""


def prepare_runner_network(docker, runner_image):
    inspected = docker(["network", "inspect", RUNNER_NETWORK], timeout=15)
    if inspected.returncode != 0:
        created = docker(["network", "create", "--opt", "com.docker.network.bridge.name=rag-exec0", RUNNER_NETWORK], timeout=15)
        if created.returncode != 0 and docker(["network", "inspect", RUNNER_NETWORK], timeout=15).returncode != 0:
            raise HTTPException(503, "执行网络暂时不可用")
    secured = docker(["run", "--rm", "--network=host", "--cap-drop=ALL", "--cap-add=NET_ADMIN",
        "--read-only", runner_image, "sh", "-ec", NETWORK_RULES], timeout=15)
    if secured.returncode != 0:
        raise HTTPException(503, "执行网络暂时不可用")
class RunRequest(BaseModel):
    workspace_id: str
    command: str = Field(min_length=1, max_length=2000)
    files: dict[str,str] = Field(default_factory=dict)
    skill_files: dict[str,str] = Field(default_factory=dict, max_length=2000)


def input_archive(files, *, skill=False):
    archive = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for path, encoded in files.items():
            safe_path(path)
            if len(encoded) > 280_000_000:
                raise HTTPException(422, "文件输入无效")
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise HTTPException(422, "文件输入无效") from None
            total += len(data)
            if skill and total > MAX_BUNDLE_BYTES:
                raise HTTPException(413, "本次加载的 Skill 文件总大小超过限制")
            if not skill and (len(data) > 200 * 1024 * 1024 or total > 512 * 1024 * 1024):
                raise HTTPException(413, "执行输入单文件不能超过 200 MiB，总计不能超过 512 MiB")
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o555 if skill and path.lower().endswith((".sh", ".bash", ".py", ".js", ".mjs", ".cjs")) else 0o444 if skill else 0o644
            bundle.addfile(info, io.BytesIO(data))
    return archive.getvalue()


@app.post("/run")
def run(payload: RunRequest):
    volume = f"workspace-exec-{uuid.uuid4().hex}"
    runner_image = os.environ.get("WORKSPACE_RUNNER_IMAGE", "rag-workspace-executor:latest")
    def docker(args, *, input_bytes=None, timeout=30):
        return subprocess.run(["docker", *args], input=input_bytes, capture_output=True, timeout=timeout)
    archive = input_archive(payload.files)
    skills_archive = input_archive(payload.skill_files, skill=True) if payload.skill_files else None
    skills_volume = f"workspace-exec-skills-{uuid.uuid4().hex}" if skills_archive else None
    prepare_runner_network(docker, runner_image)
    try:
        created = docker(["volume", "create", volume], timeout=15)
        if created.returncode != 0:
            raise HTTPException(503, "隔离执行环境暂时不可用")
        populated = docker(["run", "--rm", "-i", "--mount", f"source={volume},target=/workspace", runner_image,
            "tar", "-x", "-C", "/workspace"], input_bytes=archive, timeout=30)
        if populated.returncode != 0:
            raise HTTPException(503, "隔离执行环境无法准备工作区")
        if skills_volume:
            if docker(["volume", "create", skills_volume], timeout=15).returncode != 0:
                raise HTTPException(503, "无法准备 Skill 文件")
            populated = docker(["run", "--rm", "-i", "--network=none", "--mount",
                f"source={skills_volume},target=/skills", runner_image, "tar", "-x", "-C", "/skills"],
                input_bytes=skills_archive, timeout=30)
            if populated.returncode != 0:
                raise HTTPException(503, "无法准备 Skill 文件")
        container_name = f"workspace-exec-run-{uuid.uuid4().hex}"
        command=["docker","run","--name",container_name,"--rm",f"--network={RUNNER_NETWORK}","--cpus=1","--memory=512m","--pids-limit=64",
            "--read-only","--cap-drop=ALL","--security-opt=no-new-privileges",
            "--mount",f"source={volume},target=/workspace,readonly=false",
            *(["--mount", f"source={skills_volume},target=/skills,readonly=true"] if skills_volume else []),
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "-w","/workspace",runner_image,"sh","-lc",payload.command]
        changed={}
        def export_files():
            exported = docker(["run", "--rm", "-i", "--mount", f"source={volume},target=/workspace,readonly=true",
                runner_image, "tar", "-c", "-C", "/workspace", "."], timeout=30)
            if exported.returncode != 0:
                return
            with tarfile.open(fileobj=io.BytesIO(exported.stdout), mode="r:") as bundle:
                for item in bundle.getmembers():
                    if item.isfile() and item.size <= 200 * 1024 * 1024:
                        path = item.name.removeprefix("./")
                        safe_path(path)
                        handle = bundle.extractfile(item)
                        if handle: changed[path]=base64.b64encode(handle.read()).decode()
        try:
            completed=subprocess.run(command,text=True,capture_output=True,timeout=60)
        except subprocess.TimeoutExpired:
            docker(["rm", "-f", container_name], timeout=15)
            export_files()
            return {"status":"timeout","error":"脚本执行超时，已停止", "files": changed}
        export_files()
        return {"status":"completed" if completed.returncode==0 else "failed","exit_code":completed.returncode,"stdout":completed.stdout[-12000:],"stderr":completed.stderr[-12000:],"files":changed}
    finally:
        if skills_volume:
            docker(["volume", "rm", "-f", skills_volume], timeout=15)
        docker(["volume", "rm", "-f", volume], timeout=15)
