"""Opt-in native Linux Docker/Nginx acceptance; never use a live deployment.

Build backend/Dockerfile from the current checkout, load it into an isolated
Docker daemon, install Python/Nginx/OpenSSL there, then set
RUN_PAYMENT_PROXY_CONTAINER_TESTS=1 and PAYMENT_PROXY_TEST_IMAGE to that image.
"""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import payment_proxy as proxy


@unittest.skipUnless(os.environ.get("RUN_PAYMENT_PROXY_CONTAINER_TESTS") == "1",
                     "requires an isolated native Linux Docker daemon and Nginx")
class ContainerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.image = os.environ["PAYMENT_PROXY_TEST_IMAGE"]
        self.temporary = tempfile.TemporaryDirectory(prefix="payment-proxy-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = "payment-proxy-" + secrets.token_hex(4)
        self.network = self.project + "-backend"
        self.raw(["docker", "image", "inspect", self.image])
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.raw(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                  "-keyout", str(self.root / "key.pem"), "-out", str(self.root / "cert.pem"),
                  "-days", "1", "-subj", "/CN=example.test"])
        self.nginx = self.root / "nginx.conf"
        self.nginx.write_text(f"""
pid {self.root}/nginx.pid;
error_log {self.root}/error.log warn;
events {{}}
http {{
    access_log off;
    server {{
        listen 443 ssl;
        server_name example.test;
        ssl_certificate {self.root}/cert.pem;
        ssl_certificate_key {self.root}/key.pem;
        location /api/ {{
            proxy_pass http://127.0.0.1:{self.port}/;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }}
    }}
}}
""", encoding="utf-8")
        self.nginx_args = ["nginx", "-p", str(self.root), "-c", str(self.nginx)]
        self.raw(self.nginx_args)
        self.addCleanup(lambda: self.raw([*self.nginx_args, "-s", "quit"]))
        # Use the release's real resolver without starting the business app.
        (self.root / "proxy_probe.py").write_text(
            proxy.PROBE.split("uvicorn.run(")[0], encoding="utf-8")
        self.releases = []
        self.addCleanup(self.clean_containers)

    def raw(self, command):
        result = subprocess.run([str(x) for x in command], capture_output=True, text=True, timeout=120)
        if result.returncode:
            self.fail(f"{command[0]} failed ({result.returncode}): {result.stderr[-400:]}")
        return result.stdout

    def run_command(self, command):
        if command == ["nginx", "-T"]:
            return self.raw([*self.nginx_args, "-T"])
        return self.raw(command)

    def clean_containers(self):
        for release in self.releases:
            self.raw([*self.compose(release), "down", "--remove-orphans"])
        self.raw(["docker", "network", "rm", self.network])

    def compose(self, release):
        return ["docker", "compose", "--project-name", self.project, "--project-directory", str(release),
                "-f", str(release / "compose.json")]

    def make_network(self, gateway):
        self.raw(["docker", "network", "create", "--subnet", gateway.rsplit(".", 1)[0] + ".0/24",
                  "--gateway", gateway, self.network])

    def release(self, name, trusted):
        release = self.root / name
        release.mkdir()
        self.releases.append(release)
        (release / "app.env").write_text(
            f"PAYMENT_TRUSTED_PROXY_IPS={trusted}\nAPI_PUBLIC_URL=https://example.test\n", encoding="utf-8")
        (release / "compose.json").write_text(json.dumps({
            "services": {"api": {
                "image": self.image, "env_file": ["app.env"],
                "command": ["uvicorn", "proxy_probe:app", "--host", "0.0.0.0", "--port", "8000",
                            "--no-proxy-headers", "--no-access-log", "--lifespan", "off"],
                "volumes": [f"{self.root}/proxy_probe.py:/app/proxy_probe.py:ro"],
                "ports": [f"127.0.0.1:{self.port}:8000"],
                "networks": ["backend"],
            }},
            "networks": {"backend": {"external": True, "name": self.network}},
        }), encoding="utf-8")
        return release

    def verify(self, release, *, running=False):
        proxy.validate_release(self.run_command, self.compose(release), running=running,
                               candidate_nginx=self.nginx.read_text(encoding="utf-8"))

    def assert_nginx_and_port_boundary(self, gateway):
        # This container is an untrusted external client of Nginx. Its forged
        # left XFF/alternative headers must not replace its actual source.
        script = f"""
import json,socket,ssl,urllib.request
source=socket.gethostbyname(socket.gethostname())
req=urllib.request.Request('https://{gateway}/api/',headers={{
    'Host':'example.test','X-Forwarded-For':'192.0.2.99',
    'X-Real-IP':'192.0.2.88','Forwarded':'for=192.0.2.77'}})
response=json.load(urllib.request.urlopen(req,context=ssl._create_unverified_context(),timeout=5))
assert response == {{'peer':'{gateway}','source':source}}, 'Nginx/source boundary failed'
try:
    socket.create_connection(('{gateway}',{self.port}),timeout=2)
except OSError:
    pass
else:
    raise AssertionError('loopback API port reachable through bridge gateway')
print('nginx source and loopback reachability verified')
"""
        self.raw(["docker", "run", "--rm", "--network", self.network, "--entrypoint", "python",
                  self.image, "-c", script])

    def test_first_release_drift_republish_and_rollback(self):
        gateway1, gateway2 = "10.233.133.1", "10.234.133.1"
        self.make_network(gateway1)
        old = self.release("v1", gateway1)
        self.verify(old)  # First deployment, no existing API container.
        self.raw([*self.compose(old), "up", "-d", "--pull", "never"])
        self.verify(old, running=True)
        self.assert_nginx_and_port_boundary(gateway1)

        self.raw([*self.compose(old), "down"])
        self.raw(["docker", "network", "rm", self.network])
        self.make_network(gateway2)
        with self.assertRaisesRegex(proxy.ProxyError, "does not match"):
            self.verify(old)
        empty = self.release("empty", "")
        with self.assertRaisesRegex(proxy.ProxyError, "is empty"):
            self.verify(empty)

        # A normal HTTP success does not make a stale trust setting acceptable.
        self.raw([*self.compose(old), "up", "-d", "--pull", "never"])
        deadline = time.monotonic() + 15
        while True:
            try:
                proxy.fetch_probe(f"http://127.0.0.1:{self.port}/")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        with self.assertRaisesRegex(proxy.ProxyError, "does not match"):
            self.verify(old, running=True)

        new = self.release("v2", gateway2)
        self.verify(new)
        self.raw([*self.compose(new), "up", "-d", "--pull", "never", "--force-recreate"])
        self.verify(new, running=True)
        self.assert_nginx_and_port_boundary(gateway2)
        # Old rollback snapshot is blocked before any container/link switch.
        with self.assertRaisesRegex(proxy.ProxyError, "does not match"):
            self.verify(old)
        # Explicitly correcting only the isolated old snapshot permits rollback.
        (old / "app.env").write_text(
            f"PAYMENT_TRUSTED_PROXY_IPS={gateway2}\nAPI_PUBLIC_URL=https://example.test\n", encoding="utf-8")
        self.verify(old)
        self.raw([*self.compose(old), "up", "-d", "--pull", "never", "--force-recreate"])
        self.verify(old, running=True)
        self.assert_nginx_and_port_boundary(gateway2)


if __name__ == "__main__":
    unittest.main()
