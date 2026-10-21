"""Import an operator-provided AccessKey file without printing credentials."""

import argparse
import re
from pathlib import Path

from dotenv import set_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("credentials", type=Path)
    args = parser.parse_args()
    text = args.credentials.read_text(encoding="utf-8-sig")
    key_id = re.search(r"\bLTAI[A-Za-z0-9]+\b", text)
    secret = re.search(r"(?im)^\s*AccessKeySecret\s*[:=\uff1a]?\s*(\S+)\s*$", text)
    if not key_id or not secret:
        raise SystemExit("Expected an AccessKeyId and a labeled AccessKeySecret.")
    env = Path(__file__).resolve().parents[1] / ".env"
    for name, value in {
        "ALIYUN_ACCESS_KEY_ID": key_id.group(),
        "ALIYUN_ACCESS_KEY_SECRET": secret.group(1),
        "PDF_PARSER": "aliyun",
    }.items():
        set_key(env, name, value)
    print("DocMind credentials imported into the local .env; values are hidden.")


if __name__ == "__main__":
    main()
