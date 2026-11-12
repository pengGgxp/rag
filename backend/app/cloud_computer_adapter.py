"""Alibaba Agent Sandbox via the SDK version documented by Alibaba.

SDK reference: https://help.aliyun.com/zh/agent-sandbox/using-the-cloud-sandbox-via-the-sdk
No provider credentials or SDK identifiers are exposed to the model.
"""
import json
import shlex
from contextlib import contextmanager
from datetime import datetime, timezone

PRIVATE_NETWORKS = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "198.18.0.0/15",
    "224.0.0.0/4", "240.0.0.0/4", "::1/128", "fc00::/7", "fe80::/10"]
ROOT = "/home/user/workspace"
STATE = "/tmp/zhiku-cloud-computer"
DEPENDENCIES = f"{STATE}/deps"


def image_file_client(files):
    """Use one request budget rather than e2b's cached transport's three inner retries."""
    import httpx
    config = files._connection_config
    return httpx.Client(base_url=files._envd_api_url,
        headers=config.sandbox_headers,
        transport=httpx.HTTPTransport(http2=True, proxy=config.proxy, retries=0))


@contextmanager
def image_file_transport(files, observe):
    from e2b.sandbox_sync.filesystem.filesystem import Filesystem
    if not isinstance(files, Filesystem):
        yield
        return
    previous = getattr(files._thread_local, "envd_api", None)
    with image_file_client(files) as client:
        client.event_hooks["response"].append(observe)
        files._thread_local.envd_api = client
        try:
            yield
        finally:
            client.event_hooks["response"].remove(observe)
            if previous is None:
                del files._thread_local.envd_api
            else:
                files._thread_local.envd_api = previous


def preview_ingress_port(identifier):
    from uuid import UUID
    return 51000 + UUID(str(identifier)).int % 12000

# The launcher captures output on disk rather than accumulating arbitrary model
# stdout in the API process. Its process group can be terminated independently.
LAUNCHER = '''import json, os, subprocess, sys, threading
from pathlib import Path
job = Path(sys.argv[1])
payload = json.loads((job / "input.json").read_text())
process = subprocess.Popen(["bash", "-lc", payload["command"]], cwd=payload["cwd"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
(job / "pgid").write_text(str(process.pid))
tails = {"stdout": bytearray(), "stderr": bytearray()}
def drain(pipe, name):
    for block in iter(lambda: pipe.read(4096), b""):
        tails[name].extend(block)
        del tails[name][:-12000]
threads = [threading.Thread(target=drain, args=(pipe, name)) for name, pipe in [("stdout", process.stdout), ("stderr", process.stderr)]]
for thread in threads: thread.start()
code = process.wait()
for thread in threads: thread.join()
result = {"exit_code": code, **{name: data.decode("utf-8", errors="replace") for name, data in tails.items()}}
(job / "result.tmp").write_text(json.dumps(result))
os.replace(job / "result.tmp", job / "result.json")
'''

# Service stdio goes to bounded logs instead of the command launcher's pipes.
# A durable intent precedes Popen; a lost response is probed, never replayed.
PREVIEW_LAUNCHER = '''import json, os, signal, socket, subprocess, sys, time, urllib.request
from pathlib import Path
job = Path(sys.argv[1])
payload = json.loads((job / "input.json").read_text())
result = job / "result.json"
def finish(value):
    (job / "result.tmp").write_text(json.dumps(value))
    os.replace(job / "result.tmp", result)
try:
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", payload["port"])) == 0:
            raise RuntimeError("port already in use; select a different HTTP port")
    # An exclusive marker prevents two launchers from executing the command twice.
    with (job / "launch-intent").open("x") as marker: marker.write("starting")
    if (job / "stop-requested").exists(): raise RuntimeError("preview stopped")
    process = subprocess.Popen([sys.executable, str(job / "preview-service.py"), str(job)], cwd=payload["cwd"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    (job / "service-pgid").write_text(str(process.pid))
    if (job / "stop-requested").exists(): raise RuntimeError("preview stopped")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if process.poll() is not None: raise RuntimeError("HTTP service exited before readiness")
        try:
            with opener.open("http://127.0.0.1:" + str(payload["port"]) + "/", timeout=1) as response:
                if response.status < 500: break
        except urllib.error.HTTPError as error:
            if error.code < 500: break
        except (OSError, TimeoutError): pass
        time.sleep(.25)
    else: raise RuntimeError("HTTP readiness timed out; listen on the requested port")
    finish({"exit_code": 0, "stdout": "HTTP service ready", "stderr": ""})
except FileExistsError:
    # Another durable launch owns this job; do not overwrite its result.
    pass
except Exception as error:
    if "process" in globals() and process.poll() is None:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    finish({"exit_code": 1, "stdout": "", "stderr": str(error)[:500]})
'''

PREVIEW_SERVICE = '''import json, subprocess, sys, threading
from pathlib import Path
job = Path(sys.argv[1])
payload = json.loads((job / "input.json").read_text())
process = subprocess.Popen(["bash", "-lc", payload["command"]], cwd=payload["cwd"],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
def drain(pipe, name):
    tail = bytearray()
    for block in iter(lambda: pipe.read1(4096), b""):
        tail.extend(block)
        del tail[:-65536]
        (job / name).write_bytes(tail)
threads = [threading.Thread(target=drain, args=(pipe, name), daemon=True) for pipe, name in
    [(process.stdout, "service-stdout.log"), (process.stderr, "service-stderr.log")]]
for thread in threads: thread.start()
code = process.wait()
(job / "service-exit").write_text(str(code))
'''

# Alibaba forwards its traffic header to the destination process. A root-owned
# ingress removes it before untrusted application code can inspect requests.
PREVIEW_INGRESS = '''import http.client, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
application, ingress = map(int, sys.argv[1:3])
hop = {"connection","keep-alive","transfer-encoding","te","trailer","upgrade","proxy-authorization","proxy-authenticate"}
class Handler(BaseHTTPRequestHandler):
 protocol_version = "HTTP/1.1"
 def log_message(self,*args): pass
 def proxy(self):
  connection = http.client.HTTPConnection("127.0.0.1", application, timeout=30)
  try:
   length = int(self.headers.get("content-length","0"))
   if length < 0 or length > 16777216: self.send_error(413); return
   excluded = hop | {"e2b-traffic-access-token","x-zk-preview-host","host","content-length","authorization","x-access-token"}
   headers = {key:value for key,value in self.headers.items() if key.lower() not in excluded}
   headers["Host"] = self.headers.get("x-zk-preview-host", "127.0.0.1:"+str(application))
   body = self.rfile.read(length) if length else None
   connection.request(self.command,self.path,body=body,headers=headers)
   response = connection.getresponse()
   self.send_response(response.status)
   for key,value in response.getheaders():
    if key.lower() not in hop | {"content-length","e2b-traffic-access-token"}: self.send_header(key,value)
   self.send_header("Connection","close"); self.end_headers()
   received = 0
   if self.command != "HEAD":
    while True:
     block = response.read(65536)
     if not block: break
     received += len(block)
     if received > 67108864: break
     self.wfile.write(block)
  except (OSError, http.client.HTTPException, ValueError):
   try: self.send_error(502)
   except OSError: pass
  finally:
   self.close_connection = True
   connection.close()
 do_GET=do_HEAD=do_POST=do_PUT=do_PATCH=do_DELETE=do_OPTIONS=proxy
ThreadingHTTPServer(("0.0.0.0",ingress),Handler).serve_forever()
'''

class AlibabaSandbox:
    def __init__(self, config, key):
        from e2b import Sandbox
        self.sdk = Sandbox
        domain = f"{config['region']}.sandbox.aliyuncs.com"
        self.options = {"api_key": key, "api_url": f"https://api.{domain}", "domain": domain,
                        "request_timeout": 30}
        self.config = config

    def create(self, identifier):
        metadata = {"application": "zhiku", "session": str(identifier)}
        mount = self.config.get("resource_mount")
        network = {"deny_out": PRIVATE_NETWORKS + self.config["blocked_hosts"], "allow_public_traffic": False}
        if mount:
            network["allow_out"] = [
                f"{mount['bucket']}.oss-{mount['region']}.aliyuncs.com"]
            metadata.update({"fc.sandbox.auth.role": self.config["oss_role_arn"],
                "fc.sandbox.storage.oss": json.dumps({"mountPoints": [{
                    "bucketName": mount["bucket"], "mountDir": ROOT,
                    "bucketPath": "/" + mount["prefix"].rstrip("/"),
                    "endpoint": f"https://oss-{mount['region']}.aliyuncs.com", "readOnly": False}]})})
        sandbox = self.sdk.create(template=self.config["template"], timeout=self.config["max_seconds"],
            metadata=metadata, secure=True,
            allow_internet_access=True,
            network=network,
            **self.options)
        return sandbox.sandbox_id, sandbox.get_info().started_at

    def find(self, identifier):
        from e2b import SandboxQuery
        pages = self.sdk.list(query=SandboxQuery(metadata={"application": "zhiku", "session": str(identifier)}), **self.options)
        found = []
        while pages.has_next:
            found.extend((item.sandbox_id, item.started_at) for item in pages.next_items())
        return found

    def connect(self, identifier):
        # E2B's omitted timeout means 300 seconds, which can extend a VM near
        # expiry. Explicitly preserve its existing deadline with a safety margin.
        info = self.sdk.get_info(identifier, **self.options)
        remaining = max(1, int((info.end_at-datetime.now(timezone.utc)).total_seconds())-5)
        return self.sdk.connect(identifier, timeout=remaining, **self.options)

    def exists(self, identifier):
        from e2b import SandboxNotFoundException
        try:
            self.sdk.get_info(identifier, **self.options)
            return True
        except SandboxNotFoundException:
            return False

    def kill(self, identifier):
        self.sdk.kill(identifier, **self.options)
        return not self.exists(identifier)

    def prepare(self, sandbox):
        sandbox.commands.run(f"mkdir -p {ROOT} {STATE}/jobs {DEPENDENCIES}", user="user")
        sandbox.files.write(f"{STATE}/launcher.py", LAUNCHER, user="user")

    def clear_skills(self, sandbox):
        sandbox.commands.run("rm -rf /skills && mkdir -p /skills && chmod 555 /skills", user="root")

    def write(self, sandbox, path, data, root=ROOT):
        from .workspace_files import valid_path
        path = valid_path(path)
        # Refuse symlink ancestors before writing a new resource into a reused VM.
        script = ("from pathlib import Path; p=Path(" + repr(f"{root}/{path}") + "); "
            "assert not any(q.is_symlink() for q in [p,*p.parents]); p.parent.mkdir(parents=True,exist_ok=True)")
        sandbox.commands.run("python3 -c " + shlex.quote(script), user="user")
        sandbox.files.write(f"{root}/{path}", data, user="user")

    def write_skill(self, sandbox, path, data):
        from .workspace_files import valid_path
        path = valid_path(path)
        target = f"/skills/{path}"
        script = ("from pathlib import Path; p=Path("+repr(target)+"); "
            "assert not any(q.is_symlink() for q in [p,*p.parents]); p.parent.mkdir(parents=True,exist_ok=True)")
        sandbox.commands.run("python3 -c " + shlex.quote(script), user="root")
        sandbox.files.write(target, data, user="root")
        sandbox.commands.run("chmod 555 " + shlex.quote(target), user="root")

    def remove(self, sandbox, path):
        from .workspace_files import valid_path
        script = "from pathlib import Path; Path(" + repr(f"{ROOT}/{valid_path(path)}") + ").unlink(missing_ok=True)"
        sandbox.commands.run("python3 -c " + shlex.quote(script), user="user")

    def start(self, sandbox, identifier, command):
        job = f"{STATE}/jobs/{identifier}"
        sandbox.commands.run(f"mkdir -p {job}", user="user")
        sandbox.files.write(f"{job}/input.json", json.dumps({"command": command, "cwd": ROOT}), user="user")
        handle = sandbox.commands.run(f"python3 {STATE}/launcher.py {job}", background=True, timeout=0,
            user="user", envs={"PIP_TARGET": DEPENDENCIES, "PYTHONPATH": DEPENDENCIES,
                              "npm_config_prefix": f"{STATE}/npm"})
        sandbox.files.write(f"{job}/launcher-pid", str(handle.pid), user="user")
        return handle.pid

    def start_file(self, sandbox, identifier, operation):
        from pathlib import Path
        job = f"{STATE}/jobs/{identifier}"
        sandbox.commands.run(f"mkdir -p {job}", user="user")
        sandbox.files.write(f"{job}/operation.json", json.dumps(operation), user="user")
        sandbox.files.write(f"{job}/file-tool.py", Path(__file__).with_name("cloud_file_operation.py").read_text(encoding="utf-8"), user="user")
        return self.start(sandbox, identifier, f"python3 {job}/file-tool.py {job}")

    def start_preview(self, sandbox, identifier, payload):
        job = f"{STATE}/jobs/{identifier}"
        ingress = f"{STATE}/ingress/{identifier}"
        sandbox.commands.run(f"mkdir -p {job}", user="user")
        sandbox.commands.run(f"mkdir -p {ingress}; chmod 700 {STATE}/ingress {ingress}", user="root")
        sandbox.files.write(f"{ingress}/proxy.py", PREVIEW_INGRESS, user="root")
        proxy = sandbox.commands.run(f"python3 {ingress}/proxy.py {payload['port']} {preview_ingress_port(identifier)}",
            background=True, timeout=0, user="root")
        sandbox.files.write(f"{ingress}/pid", str(proxy.pid), user="root")
        sandbox.files.write(f"{job}/input.json", json.dumps({**payload, "cwd": ROOT}), user="user")
        sandbox.files.write(f"{job}/preview-launcher.py", PREVIEW_LAUNCHER, user="user")
        sandbox.files.write(f"{job}/preview-service.py", PREVIEW_SERVICE, user="user")
        handle = sandbox.commands.run(f"python3 {job}/preview-launcher.py {job}", background=True,
            timeout=0, user="user", envs={"PYTHONPATH": DEPENDENCIES, "PIP_TARGET": DEPENDENCIES,
                "npm_config_prefix": f"{STATE}/npm"})
        sandbox.files.write(f"{job}/launcher-pid", str(handle.pid), user="user")
        return handle.pid

    def preview_target(self, sandbox, port, identifier):
        import httpx
        host, token = sandbox.get_host(preview_ingress_port(identifier)), sandbox.traffic_access_token
        if not token:
            raise RuntimeError("authenticated preview traffic is unavailable")
        with httpx.stream("GET", f"https://{host}/", headers={"e2b-traffic-access-token": token},
                timeout=10, follow_redirects=False) as response:
            if response.status_code >= 500:
                raise RuntimeError("HTTP ingress is not ready")
        return host, token

    def preview_alive(self, sandbox, identifier):
        script = ("import os\nfrom pathlib import Path\np=Path(" + repr(f"{STATE}/jobs/{identifier}/service-pgid") + ")\n"
            "try:\n assert not p.with_name('service-exit').exists(); os.kill(int(p.read_text()),0); print('alive')\nexcept (OSError,ValueError,AssertionError): print('dead')\n")
        return sandbox.commands.run("python3 -c " + shlex.quote(script), user="user").stdout.strip() == "alive"

    def stop_preview(self, sandbox, identifier):
        sandbox.files.write(f"{STATE}/jobs/{identifier}/stop-requested", "stop", user="user")
        script = ("import os,signal\nfrom pathlib import Path\np=Path(" + repr(f"{STATE}/jobs/{identifier}/service-pgid") + ")\n"
            "try:\n if p.exists(): os.killpg(int(p.read_text()),signal.SIGKILL)\nexcept ProcessLookupError: pass\n")
        sandbox.commands.run("python3 -c " + shlex.quote(script), user="user")
        root_script = ("import os,signal\nfrom pathlib import Path\np=Path(" + repr(f"{STATE}/ingress/{identifier}/pid") + ")\n"
            "try:\n if p.exists(): os.kill(int(p.read_text()),signal.SIGKILL)\nexcept ProcessLookupError: pass\n")
        sandbox.commands.run("python3 -c " + shlex.quote(root_script), user="root")

    def file_result(self, sandbox, identifier):
        return json.loads(sandbox.files.read(f"{STATE}/jobs/{identifier}/file-result.json", user="user"))

    def read_file(self, sandbox, path, *, max_bytes, owner="user", check_active=None):
        """Read at most max_bytes without copying the body into a command result."""
        import httpx
        from e2b import (FileNotFoundException, TimeoutException, RateLimitException, SandboxException,
            InvalidArgumentException, AuthenticationException)
        from .image_reading import ImageReadError
        from .upstream_retries import RetryBudget, TRANSIENT_STATUS, retry_after, retry_scope

        # e2b 2.31 maps 500/503 to SandboxException without structured HTTP metadata.
        # A temporary response hook on its thread-local client preserves only status/Retry-After.
        observed = {}
        def observe(response):
            if response.request.method == "GET" and response.request.url.path == "/files":
                observed.update(status=response.status_code, after=retry_after(response.headers.get("Retry-After")),
                    timeout=response.request.extensions.get("timeout"))
        with image_file_transport(sandbox.files, observe):
            with retry_scope(check_active):
                budget = RetryBudget("sandbox_file")
                category = "transfer"
                while budget.begin():
                    data = bytearray()  # A failed attempt never contributes bytes to the next one.
                    observed.clear()
                    try:
                        with sandbox.files.read(path, format="stream", user=owner,
                                request_timeout=budget.remaining(), stream_idle_timeout=budget.remaining()) as stream:
                            iterator = iter(stream)
                            while True:
                                budget.check()
                                if not budget.remaining():
                                    raise ImageReadError("图片读取等待已超时", category="deadline")
                                if isinstance(observed.get("timeout"), dict):
                                    observed["timeout"]["read"] = budget.remaining()
                                try:
                                    block = next(iterator)
                                except StopIteration:
                                    break
                                budget.check()
                                if len(data) + len(block) > max_bytes:
                                    raise ValueError("图片超过大小限制")
                                data.extend(block)
                        budget.check()
                        if not budget.remaining():
                            raise ImageReadError("图片读取等待已超时", category="deadline")
                        return bytes(data)
                    except FileNotFoundException:
                        raise ImageReadError("图片文件不存在", category="missing") from None
                    except (httpx.TransportError, TimeoutException):
                        category, after = "transfer", None
                    except SandboxException as error:
                        status = observed.get("status")
                        if isinstance(error, AuthenticationException) or status in (401, 403):
                            if isinstance(error, AuthenticationException):
                                raise AuthenticationException("图片访问权限已失效") from None
                            from fastapi import HTTPException
                            raise HTTPException(status, "图片访问权限已失效") from None
                        if isinstance(error, InvalidArgumentException):
                            raise ImageReadError("图片读取请求无效", category="request") from None
                        if isinstance(error, RateLimitException):
                            status = 429
                        elif type(error) is not SandboxException or status is None:
                            raise  # Unknown SDK errors and permission exceptions retain their semantics.
                        if status not in TRANSIENT_STATUS:
                            raise ImageReadError("图片读取请求未能完成", category="request") from None
                        category, after = f"http_{status}", observed.get("after")
                    delay = budget.delay(category, after)
                    if delay is None:
                        raise ImageReadError("图片传输重试已耗尽", category=category) from None
                    budget.wait(delay)
                raise ImageReadError("图片读取等待已超时", category="deadline") from None

    def poll(self, sandbox, identifier):
        path = f"{STATE}/jobs/{identifier}/result.json"
        if sandbox.files.exists(path, user="user"):
            return json.loads(sandbox.files.read(path, user="user"))
        pid_path = f"{STATE}/jobs/{identifier}/launcher-pid"
        if sandbox.files.exists(pid_path, user="user"):
            pid = int(sandbox.files.read(pid_path, user="user"))
            if pid not in {process.pid for process in sandbox.commands.list()}:
                if sandbox.files.exists(path, user="user"):
                    return json.loads(sandbox.files.read(path, user="user"))
                raise RuntimeError("cloud launcher exited without a durable result")
        return None

    def cancel(self, sandbox, identifier, process_id):
        job = f"{STATE}/jobs/{identifier}"
        script = ("import os,signal\nfrom pathlib import Path\np=Path(" + repr(job + "/pgid") + ")\n"
                  "try:\n    if p.exists(): os.killpg(int(p.read_text()),signal.SIGKILL)\n"
                  "except ProcessLookupError: pass\n")
        sandbox.commands.run("python3 -c " + shlex.quote(script), user="user")
        if process_id:
            sandbox.commands.kill(process_id)

def adapter(config, encrypted_key):
    from .site_auth import reveal
    return AlibabaSandbox(config, reveal(encrypted_key))
