"""Server-side maintenance control client.

This script talks only to the API's loopback-only control endpoint and never
prints the control token.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def request(method: str, base_url: str, action: str | None = None) -> dict:
    token = os.environ.get("DEPLOY_CONTROL_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("DEPLOY_CONTROL_TOKEN is missing or too short")
    payload = None if action is None else json.dumps({"active": action == "on"}).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/internal/deploy/maintenance",
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-RAG-Request": "1",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "on", "off"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        result = request("GET" if args.action == "status" else "PUT", args.base_url, args.action)
    except Exception as exc:
        print(f"maintenance control failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
