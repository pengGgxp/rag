"""Synthetic model/provider; commands run real subprocesses in test-only storage."""
import json
import os
import signal
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.cloud_computer.cloud_file_operation import perform

app = FastAPI()
instances, jobs = {}, {}
metrics = {"creates": 0, "starts": 0, "kills": 0, "model_steps": 0, "requests": []}
guard = threading.Lock()


def launch(body, command):
    instance = instances[body["sandbox"]]
    root = Path("/synthetic/objects") / instance["config"]["resource_mount"]["prefix"]
    root.mkdir(parents=True, exist_ok=True)
    job = {"sandbox": body["sandbox"], "result": None}
    process = subprocess.Popen(["bash", "-lc", command], cwd=root, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    job["process"] = process
    jobs[body["identifier"]] = job
    metrics["starts"] += 1

    def finish():
        stdout, stderr = process.communicate()
        job["result"] = {"exit_code": process.returncode,
                        "stdout": stdout.decode(errors="replace"),
                        "stderr": stderr.decode(errors="replace")}

    threading.Thread(target=finish, daemon=True).start()
    return process.pid


@app.post("/provider/{action}")
def provider(action: str, body: dict):
    identifier = body.get("identifier")
    if action == "create":
        with guard:
            metrics["creates"] += 1
            instances[identifier] = {"id": identifier, "started_at": datetime.now(timezone.utc).isoformat(),
                                     "config": body["config"], "alive": True}
        return {key: instances[identifier][key] for key in ("id", "started_at")}
    if action == "find":
        return [{key: item[key] for key in ("id", "started_at")} for item in instances.values()
                if item["id"] == identifier and item["alive"]]
    if action == "exists":
        return bool(instances.get(identifier, {}).get("alive"))
    if action == "kill":
        metrics["kills"] += 1
        instances[identifier]["alive"] = False
        for job in jobs.values():
            if job["sandbox"] == identifier and job.get("process") and job["process"].poll() is None:
                os.killpg(job["process"].pid, signal.SIGKILL)
        return True
    if action == "start":
        return launch(body, body["command"])
    if action == "file":
        root = Path("/synthetic/objects") / body["config"]["resource_mount"]["prefix"]
        root.mkdir(parents=True, exist_ok=True)
        try:
            value = {"value": perform(body["operation"], root)}
        except Exception as error:
            value = {"error": str(error), "status_code": 404 if isinstance(error, FileNotFoundError) else 422}
        jobs[identifier] = {"sandbox": body["sandbox"], "file_result": value,
                           "result": {"exit_code": 0, "stdout": "", "stderr": ""}}
        metrics["starts"] += 1
        return 0
    if action == "preview":
        pid = launch(body, body["payload"]["command"])
        for _ in range(50):
            import httpx
            try:
                if httpx.get(f"http://127.0.0.1:{body['payload']['port']}/", timeout=.5).status_code < 500:
                    jobs[identifier]["result"] = {"exit_code": 0, "stdout": "HTTP ready", "stderr": ""}
                    return pid
            except Exception:
                pass
            time.sleep(.1)
        raise HTTPException(503, "Synthetic HTTP readiness failed")
    job = jobs.get(identifier)
    if not job:
        if action in ("poll", "cancel"):
            return None
        raise HTTPException(404)
    if job["sandbox"] != body["sandbox"]:
        raise HTTPException(409, "Wrong instance")
    if action == "poll":
        return job["result"]
    if action == "file_result":
        return job["file_result"]
    if action == "preview_alive":
        return bool(job.get("process") and job["process"].poll() is None)
    if action == "cancel":
        if job.get("process") and job["process"].poll() is None:
            os.killpg(job["process"].pid, signal.SIGKILL)
        return True
    raise HTTPException(404)


@app.get("/metrics")
def read_metrics():
    return {**metrics, "instances": [
        {key: item[key] for key in ("id", "alive")} for item in instances.values()]}


@app.get("/frame/{port}/{path:path}")
def frame(port: int, path: str):
    import httpx
    response = httpx.get(f"http://127.0.0.1:{port}/{path}", timeout=10)
    return Response(response.content, status_code=response.status_code,
                    media_type=response.headers.get("content-type", "text/html"))


def reply(body):
    messages = body.get("messages", [])
    names = [item["function"]["name"] for item in body.get("tools", [])]
    results = [item for item in messages if item["role"] == "tool"]
    metrics["model_steps"] += 1
    system = " ".join(str(item.get("content", "")) for item in messages if item["role"] == "system")
    metrics["requests"].append({"offered": names, "results": len(results),
        "cleanup_prompt": "临时执行任务完成且环境没有后续用途" in system,
        "keep_preview_prompt": "仍需用户交互的网页预览保留运行" in system})
    if not names:
        return "多实例验收", None, None
    decoded = [(item["name"], json.loads(item["content"])) for item in results]
    if decoded and decoded[-1][1].get("error"):
        return "验收失败：" + str(decoded[-1][1]["error"]), None, None
    created = [data["computer_id"] for name, data in decoded if name == "create_cloud_computer"]
    if len(created) < 2:
        return None, "create_cloud_computer", {"name": f"复测任务{'甲' if not created else '乙'}"}
    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Issue118 Retest</title>
<style>body{font:16px system-ui;margin:32px;color:#15261e;background:#f7f9f8}button{padding:12px 20px;cursor:pointer}</style>
<h1>算法页面复测</h1><button id="step">测试交互</button><script>
document.getElementById("step").onclick=function(){this.textContent="交互成功"};
</script></html>"""
    generate = "from pathlib import Path; Path('issue118-retest').mkdir(exist_ok=True); Path('issue118-retest/index.html').write_text(" + repr(html) + ")"
    plan = [
        ("write_file", {"computer_id": created[0], "path": "issue118-retest/shared.txt", "content": "shared-from-alpha\n"}),
        ("read_file", {"computer_id": created[1], "path": "issue118-retest/shared.txt"}),
        ("run_command", {"computer_id": created[0], "command":
            "python -c " + shlex.quote(generate)}),
        ("run_command", {"computer_id": created[1], "command": "cat issue118-retest/shared.txt; printf beta-command"}),
        ("start_preview", {"computer_id": created[0], "command": "python -m http.server 8398 --bind 0.0.0.0 --directory issue118-retest",
                           "port": 8398, "name": "Issue118 复测页面 · 甲实例"}),
        ("get_cloud_computer", {"computer_id": created[0]}),
        ("list_cloud_computers", {"active_only": True}),
        ("end_cloud_computer", {"computer_id": created[1]}),
    ]
    index = len(results) - 2
    if index < len(plan):
        name, arguments = plan[index]
        return None, name, arguments
    return "交互页面已完成，包含可点击的演示按钮。请打开预览查看；文件位于 issue118-retest/index.html。临时处理已完成。", None, None


@app.post("/v1/chat/completions")
async def complete(request: Request):
    text, name, arguments = reply(await request.json())
    message = {"role": "assistant", "content": text}
    if name:
        message["tool_calls"] = [{"id": "synthetic-call", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}

    async def events():
        delta = {"role": "assistant", "content": text}
        if name:
            delta["tool_calls"] = [{"index": 0, **message["tool_calls"][0]}]
        for chunk in [
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if name else "stop"}], "usage": usage},
        ]:
            yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
