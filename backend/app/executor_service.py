import base64, os, shutil, subprocess, tempfile
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
    root=Path(tempfile.mkdtemp(prefix="workspace-exec-"))
    try:
        for path, encoded in payload.files.items():
            target=(root / path).resolve()
            if not str(target).startswith(str(root.resolve())+os.sep) or len(encoded)>280_000_000: raise HTTPException(422,"文件输入无效")
            target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(base64.b64decode(encoded))
        command=["docker","run","--rm","--network=bridge","--cpus=1","--memory=512m","--pids-limit=64","--read-only","-v",f"{root}:/workspace","-w","/workspace","python:3.12-slim","sh","-lc",payload.command]
        try: completed=subprocess.run(command,text=True,capture_output=True,timeout=60)
        except subprocess.TimeoutExpired: return {"status":"timeout","error":"脚本执行超时，已停止"}
        changed={}
        if completed.returncode == 0:
            for item in root.rglob("*"):
                if item.is_file() and item.stat().st_size <= 5_000_000:
                    changed[str(item.relative_to(root)).replace("\\", "/")]=base64.b64encode(item.read_bytes()).decode()
        return {"status":"completed" if completed.returncode==0 else "failed","exit_code":completed.returncode,"stdout":completed.stdout[-12000:],"stderr":completed.stderr[-12000:],"files":changed}
    finally: shutil.rmtree(root,ignore_errors=True)
