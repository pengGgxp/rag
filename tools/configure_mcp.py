"""Provision a persistent local MCP credential key without printing it."""

from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key


def main():
    env = Path(__file__).resolve().parents[1] / ".env"
    if dotenv_values(env).get("MCP_CREDENTIAL_KEY"):
        print("Existing MCP credential key preserved.")
        return
    set_key(env, "MCP_CREDENTIAL_KEY", Fernet.generate_key().decode())
    print("MCP credential key saved in .env. Back up this file securely.")


if __name__ == "__main__":
    main()
