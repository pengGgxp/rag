"""Fail-closed checks for the host Nginx -> loopback Docker port topology.

This module never learns trust from requests or changes app.env. Only an exact
gateway of the explicitly selected, local bridge can be trusted. A disposable
copy of the release image checks that the host actually arrives as that peer.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path
import urllib.parse
import urllib.request


class ProxyError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ProxyError("payment proxy: " + message)


def nginx_blocks(text):
    """Read directives/blocks, including nginx -T's expanded included files.

    Includes remain explicit directives. Only the small, repository-owned API
    location is accepted; hidden includes/rewrites in that route fail closed.
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars="{};")
    lexer.whitespace_split = True
    tokens = []
    for token in lexer:
        tokens.extend(token if re.fullmatch(r"[{};]+", token) else [token])
    root, stack, words = [], [], []
    current = root
    for token in tokens:
        if token == "{":
            child = []
            current.append((words, child))
            stack.append(current)
            current, words = child, []
        elif token == "}":
            require(not words and stack, "cannot parse Nginx blocks; inspect nginx -T")
            current, words = stack.pop(), []
        elif token == ";":
            current.append((words, None))
            words = []
        else:
            words.append(token)
    require(not words and not stack, "cannot parse Nginx directives; inspect nginx -T")
    return root


def validate_nginx(text, hostname, port):
    def servers(nodes):
        for words, child in nodes:
            if words == ["server"] and child is not None:
                yield child
            elif child is not None:
                yield from servers(child)

    blocks = nginx_blocks(text)
    def check_realip(nodes, *, inherited=False):
        for words, child in nodes:
            if words and words[0] == "proxy_bind":
                require(words == ["proxy_bind", "off"],
                        "proxy_bind changes the checked host source; use the default loopback upstream connection")
            if words and words[0] == "set_real_ip_from":
                try:
                    network = ipaddress.ip_network(words[1], strict=False)
                except (ValueError, IndexError):
                    raise ProxyError("payment proxy: realip trust must use explicit controlled IP/CIDR") from None
                require(network.prefixlen > 0, "Nginx realip must not trust every source")
            if child is not None and (not inherited or words != ["server"]):
                check_realip(child, inherited=inherited)
    check_realip(blocks, inherited=True)
    candidates = []
    for server in servers(blocks):
        directives = [w for w, c in server if c is None]
        if not any(w and w[0] == "server_name" and hostname in w[1:] for w in directives):
            continue
        if not any(w and w[0] == "listen" and "ssl" in w and w[1].split(":")[-1] == "443" for w in directives):
            continue
        candidates.append(server)
    require(len(candidates) == 1, "expected one explicit HTTPS server for API_PUBLIC_URL; inspect nginx -T")
    server = candidates[0]
    check_realip(server)
    # An include or rewrite can override the checked location/header behavior.
    require(not any(w and w[0] in {"include", "rewrite", "return", "if"} for w, _ in server),
            "API server has unverified routing; use the explicit repository API location")
    locations = [(w, c) for w, c in server if w and w[0] == "location"]
    routes = [c for w, c in locations if w in (["location", "/api/"], ["location", "^~", "/api/"])]
    require(len(routes) == 1, "expected one explicit /api/ location")
    route = routes[0]
    require(route is not None and all(c is None for _, c in route), "nested API routing is not verified")
    require(not any(w and w[0] in {"include", "rewrite", "try_files", "return"} for w, _ in route),
            "API location must proxy directly to the checked loopback port")
    for words, _ in locations:
        if any(x in {"~", "~*"} for x in words):
            require(["location", "^~", "/api/"] in [w for w, _ in locations],
                    "regex location can override /api/; make /api/ authoritative")
        if len(words) > 1 and words[-1].startswith("/api/") and words[-1] != "/api/":
            require(words[-1] in {"/api/internal/", "/api/health"}, "unverified more-specific API location")
    passes = [w[1:] for w, _ in route if w and w[0] == "proxy_pass"]
    require(passes == [[f"http://127.0.0.1:{port}/"]], "Nginx API upstream must match the Compose loopback port")
    xff = [w[2:] for w, _ in route if len(w) >= 2 and w[0] == "proxy_set_header" and w[1].lower() == "x-forwarded-for"]
    require(xff in ([["$remote_addr"]], [["$proxy_add_x_forwarded_for"]]),
            "API X-Forwarded-For must overwrite or append the real connection source")


def api_contract(config):
    api = config.get("services", {}).get("api", {})
    command = api.get("command", [])
    require(isinstance(command, list) and bool(command) and command[0] == "uvicorn"
            and "--no-proxy-headers" in command and "--proxy-headers" not in command,
            "API must preserve the ASGI transport peer with --no-proxy-headers")
    require(not api.get("entrypoint"), "API must not override the checked Uvicorn entrypoint")
    require(not api.get("network_mode"), "API must use the explicit bridge network")
    ports = api.get("ports", [])
    require(len(ports) == 1, "API must publish exactly one loopback port")
    binding = ports[0]
    require(binding.get("host_ip") == "127.0.0.1" and binding.get("protocol", "tcp") == "tcp"
            and int(binding.get("target", 0)) == 8000, "API port must bind only 127.0.0.1:<port>:8000/tcp")
    require(str(binding.get("published", "")).isdigit(), "API requires an explicit loopback port")
    networks = api.get("networks", {})
    require(len(networks) == 1, "API must have exactly one explicitly selected bridge network")
    key = next(iter(networks))
    network = config.get("networks", {}).get(key, {})
    require(bool(network.get("name")), "Compose network needs an explicit resolved name")
    environment = api.get("environment", {})
    trust = environment.get("PAYMENT_TRUSTED_PROXY_IPS", "") or ""
    require(bool(trust.strip()), "PAYMENT_TRUSTED_PROXY_IPS is empty; configure the checked bridge gateway in protected app.env")
    try:
        trusted = [ipaddress.ip_network(x.strip(), strict=False) for x in trust.split(",")]
    except ValueError:
        raise ProxyError("payment proxy: invalid PAYMENT_TRUSTED_PROXY_IPS; use one exact bridge gateway") from None
    require(len(trusted) == 1 and trusted[0].prefixlen == trusted[0].max_prefixlen,
            "trust must contain exactly one gateway host address, never a subnet or additional proxies")
    return api, network["name"], str(trusted[0].network_address), int(binding["published"])


def bridge_contract(network, trusted):
    require(network.get("Driver") == "bridge" and network.get("Scope") == "local" and not network.get("Internal"),
            "selected network must be a local, non-internal Docker bridge")
    options = network.get("Options") or {}
    require(all(options.get(f"com.docker.network.bridge.gateway_mode_{family}", "nat") == "nat" for family in ("ipv4", "ipv6")),
            "bridge must use NAT gateway mode")
    gateways = [entry.get("Gateway") for entry in network.get("IPAM", {}).get("Config", [])]
    require(trusted in gateways, "trusted address does not match the selected network gateway; inspect docker network, correct protected app.env, then retry")


# The release image supplies the real resolver. No app startup, DB, secrets,
# payment requests or extra endpoint in the running application are involved.
PROBE = r'''
import json, os, uvicorn
from starlette.requests import Request
from app.client_ip import payment_client_ip
async def app(scope, receive, send):
    payload = json.dumps({"peer": scope["client"][0], "source": payment_client_ip(Request(scope), os.environ["PAYMENT_TRUSTED_PROXY_IPS"])}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": payload})
uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=False, access_log=False, log_level="error", lifespan="off")
'''


def fetch_probe(url):
    request = urllib.request.Request(url, headers={
        "X-Forwarded-For": "192.0.2.99, 198.51.100.10", "X-Real-IP": "192.0.2.88", "Forwarded": "for=192.0.2.77"})
    # Local checks must not use an operator's HTTP(S)_PROXY environment.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
        return json.load(response)


def probe_transport(run, image, network, trusted):
    name = "zhiku-payment-proxy-check-" + secrets.token_hex(6)
    try:
        run(["docker", "run", "--pull", "never", "-d", "--name", name, "--network", network,
             "-p", "127.0.0.1::8000", "--env", "PAYMENT_TRUSTED_PROXY_IPS=" + trusted,
             "--entrypoint", "python", image, "-u", "-c", PROBE])
        inspected = json.loads(run(["docker", "inspect", name]))[0]
        port = inspected["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]
        deadline = time.monotonic() + 20
        while True:
            try:
                response = fetch_probe(f"http://127.0.0.1:{port}/")
                break
            except (OSError, ValueError):
                require(time.monotonic() < deadline, "temporary source probe did not start; inspect image/network and retry")
                time.sleep(0.2)
        require(response == {"peer": trusted, "source": "198.51.100.10"},
                "host TCP peer or release resolver does not match trusted topology; do not whitelist the observed source")
        # A container on the same bridge can reach the API directly. That
        # source must remain untrusted, even when it forges every proxy header.
        address = inspected["NetworkSettings"]["Networks"][network]["IPAddress"]
        script = ("import urllib.request,json; r=urllib.request.Request(" + repr(f"http://{address}:8000/")
                  + ",headers={'X-Forwarded-For':'192.0.2.99','X-Real-IP':'192.0.2.88','Forwarded':'for=192.0.2.77'});"
                  + "print(urllib.request.urlopen(r,timeout=5).read().decode())")
        direct = json.loads(run(["docker", "run", "--pull", "never", "--rm", "--network", network, "--entrypoint", "python", image, "-c", script]))
        require(direct.get("peer") != trusted and direct.get("peer") == direct.get("source"),
                "untrusted bridge traffic can impersonate a proxy; inspect Docker routing before release")
    finally:
        original_error = sys.exc_info()[0] is not None
        try:
            run(["docker", "rm", "-f", name])
        except Exception:
            if not original_error:
                raise


def validate_release(run, compose, *, nginx_text=None, candidate_nginx=None, running=False):
    config = json.loads(run([*compose, "config", "--format", "json"]))
    api, network, trusted, port = api_contract(config)
    try:
        inspected = json.loads(run(["docker", "network", "inspect", network]))[0]
    except Exception:
        raise ProxyError("payment proxy: selected Docker bridge cannot be inspected; explicitly create/select the intended network and retry") from None
    bridge_contract(inspected, trusted)
    url = urllib.parse.urlsplit(api.get("environment", {}).get("API_PUBLIC_URL", ""))
    require(url.scheme == "https" and bool(url.hostname) and url.port in (None, 443),
            "API_PUBLIC_URL must identify the HTTPS Nginx entry on port 443")
    validate_nginx(nginx_text if nginx_text is not None else run(["nginx", "-T"]), url.hostname, port)
    if candidate_nginx is not None:
        validate_nginx(candidate_nginx, url.hostname, port)
    if running:
        identifier = run([*compose, "ps", "-q", "api"]).strip()
        require(bool(identifier), "API container is not running")
        runtime = json.loads(run(["docker", "inspect", identifier]))[0]
        env = dict(x.split("=", 1) for x in runtime["Config"]["Env"] if "=" in x)
        actual = {"services": {"api": {
            "command": runtime["Config"]["Cmd"], "environment": env,
            "entrypoint": runtime["Config"].get("Entrypoint"),
            "network_mode": runtime["HostConfig"].get("NetworkMode") if runtime["HostConfig"].get("NetworkMode") in {"host", "none"} else None,
            "ports": [{"host_ip": b["HostIp"], "published": b["HostPort"], "target": int(p.split("/")[0]), "protocol": p.split("/")[1]}
                      for p, bindings in runtime["HostConfig"]["PortBindings"].items() for b in (bindings or [])],
            "networks": runtime["NetworkSettings"]["Networks"],
        }}, "networks": {network: {"name": network}}}
        _, actual_network, actual_trust, actual_port = api_contract(actual)
        require((actual_network, actual_trust, actual_port) == (network, trusted, port)
                and runtime["Config"]["Image"] == api["image"],
                "running API differs from release proxy contract; recreate API using the checked release")
        require(runtime["NetworkSettings"]["Networks"][network]["NetworkID"] == inspected["Id"],
                "running API is attached to a replaced network; recreate it before acceptance")
    probe_transport(run, api["image"], network, trusted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    parser.add_argument("--candidate-nginx", type=Path)
    parser.add_argument("--running", action="store_true")
    args = parser.parse_args()
    compose = ["docker", "compose", "--project-directory", str(args.compose.resolve().parent)]
    for env in args.env_file:
        compose.extend(["--env-file", str(env.resolve())])
    compose.extend(["-f", str(args.compose.resolve())])

    def run(command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        # Compose's rendered config can contain credentials. Never echo it,
        # request/response bodies or an arbitrary command's diagnostic output.
        require(result.returncode == 0, f"{command[0]} command failed; inspect local service/config diagnostics and retry")
        return result.stdout

    try:
        validate_release(run, compose, running=args.running,
                         candidate_nginx=args.candidate_nginx.read_text(encoding="utf-8") if args.candidate_nginx else None)
        print("payment proxy topology and transport verified")
        return 0
    except (ProxyError, OSError, ValueError, KeyError, IndexError, TypeError, subprocess.TimeoutExpired) as exc:
        # Unexpected malformed inspection data is also a failure, never a pass.
        print(str(exc) if isinstance(exc, ProxyError) else "payment proxy: incomplete Docker/Nginx inspection; inspect local diagnostics and retry", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
