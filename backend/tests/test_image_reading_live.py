"""Opt-in image transport probe against an existing, explicitly supplied sandbox."""
import hashlib
import inspect
import json
import os
import shlex
from importlib.metadata import version
from uuid import uuid4

import pytest

from app.cloud_computer_adapter import AlibabaSandbox
from app.cloud_file_operation import image_path, inspect_image
from app.image_reading import image_type


@pytest.mark.skipif(os.environ.get("RAG_IMAGE_READ_LIVE_ACCEPT") != "1",
    reason="requires explicit access to an existing sandbox; never creates a paid instance")
def test_existing_sandbox_generates_and_streams_synthetic_image():
    provider = AlibabaSandbox({"region": os.environ["RAG_IMAGE_READ_REGION"]},
        os.environ["RAG_CLOUD_CHECK_KEY"])
    sandbox = provider.connect(os.environ["RAG_IMAGE_READ_SANDBOX_ID"])
    path = f"/tmp/zhiku-image-check-{uuid4().hex}.png"
    # Generate bytes inside the sandbox, then use the same metadata inspector as read_image.
    script = "\n".join([
        "import hashlib,json,stat,struct,zlib",
        "from pathlib import Path",
        "MAX_IMAGE_BYTES=10*1024*1024",
        inspect.getsource(image_path),
        inspect.getsource(inspect_image),
        "def chunk(kind,data): return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))",
        "image=b'\\x89PNG\\r\\n\\x1a\\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))"
        "+chunk(b'IDAT',zlib.compress(b'\\x00\\xff\\x00\\x00'))+chunk(b'IEND',b'')",
        f"Path({path!r}).write_bytes(image)",
        f"print(json.dumps(inspect_image({path!r})))",
    ])
    try:
        result = sandbox.commands.run("python3 -c " + shlex.quote(script), user="user", timeout=30)
        assert result.exit_code == 0, "Synthetic image generation/inspection failed"
        metadata = json.loads(result.stdout)
        data = provider.read_file(sandbox, path, max_bytes=10 * 1024 * 1024)
        assert len(data) == metadata["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == metadata["sha256"]
        assert image_type(data) == ("image/png", 1, 1)
        print(json.dumps({"sdk": "e2b", "version": version("e2b"),
            "size_bytes": len(data), "sha256": metadata["sha256"],
            "content_type": "image/png", "width": 1, "height": 1}))
    finally:
        cleanup = f"from pathlib import Path; Path({path!r}).unlink(missing_ok=True)"
        result = sandbox.commands.run("python3 -c " + shlex.quote(cleanup), user="user", timeout=30)
        assert result.exit_code == 0, "Synthetic image cleanup failed"
