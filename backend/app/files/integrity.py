"""计算文件内容的大小和 SHA-256 摘要。"""

import hashlib


def stream_fingerprint(stream):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def object_fingerprint(storage, key):
    with storage.download(key) as stream:
        return stream_fingerprint(stream)
