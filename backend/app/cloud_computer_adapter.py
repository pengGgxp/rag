"""Alibaba Agent Sandbox via the SDK version documented by Alibaba.

SDK reference: https://help.aliyun.com/zh/agent-sandbox/using-the-cloud-sandbox-via-the-sdk
No provider credentials or SDK identifiers are exposed to the model.
"""
import hashlib
import json
import shlex
from datetime import datetime, timezone

PRIVATE_NETWORKS = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "198.18.0.0/15",
    "224.0.0.0/4", "240.0.0.0/4", "::1/128", "fc00::/7", "fe80::/10"]
ROOT = "/home/user/workspace"
STATE = "/tmp/zhiku-cloud-computer"
DEPENDENCIES = f"{STATE}/deps"
MAX_FILE = 200 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024

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

MANIFEST = '''import hashlib,json,os,stat
from pathlib import Path
root=Path("/home/user/workspace")
assert not root.is_symlink(), "workspace must be a directory"
result={}; total=0
for directory, dirs, files in os.walk(root, followlinks=False):
    dirs[:]=[name for name in dirs if name not in {"node_modules", ".venv", "venv", "__pycache__", ".cache", ".git"} and not Path(directory,name).is_symlink()]
    for name in files:
        path=Path(directory,name)
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode): continue
        if info.st_size>209715200: raise RuntimeError("output file exceeds 200 MiB")
        total+=info.st_size
        if total>536870912 or len(result)>=2000: raise RuntimeError("output bundle too large")
        digest=hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda:handle.read(1048576),b""): digest.update(block)
        result[str(path.relative_to(root))]={"sha256":digest.hexdigest(),"size":info.st_size}
print(json.dumps(result))
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
            network["allow_out"] = [f"{mount['bucket']}.oss-{mount['region']}.aliyuncs.com", "8.8.8.8"]
            metadata.update({"fc.sandbox.auth.role": self.config["oss_role_arn"],
                "fc.sandbox.storage.oss": json.dumps({"mountPoints": [{
                    "bucketName": mount["bucket"], "mountDir": ROOT,
                    "bucketPath": "/" + mount["prefix"].rstrip("/"),
                    "endpoint": f"https://oss-{mount['region']}.aliyuncs.com", "readOnly": False}]})})
        sandbox = self.sdk.create(template=self.config["template"], timeout=self.config["max_seconds"],
            metadata=metadata, secure=True,
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

    def manifest(self, sandbox):
        result = sandbox.commands.run("python3 -c " + shlex.quote(MANIFEST), user="user")
        return json.loads(result.stdout)

    def read(self, sandbox, path, expected):
        from .workspace_files import valid_path
        data = sandbox.files.read(f"{ROOT}/{valid_path(path)}", format="bytes", user="user")
        if len(data) > MAX_FILE or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("output changed during transfer")
        return data


def adapter(config, encrypted_key):
    from .site_auth import reveal
    return AlibabaSandbox(config, reveal(encrypted_key))
