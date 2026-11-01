"""Native COS operations. Presigned URLs and SDK exceptions stay out of logs."""
import logging
from tempfile import TemporaryFile

from fastapi import HTTPException
from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosServiceError

from .site_auth import reveal

logging.getLogger("qcloud_cos").setLevel(logging.CRITICAL)


class CosStorage:
    def __init__(self, settings):
        if not settings or not settings.encrypted_secret_id or not settings.encrypted_secret_key:
            raise HTTPException(503, "文件存储尚未配置，请联系站点管理员")
        self.bucket = settings.bucket
        self.region = settings.region
        self.sdk = CosS3Client(CosConfig(Region=self.region,
            SecretId=reveal(settings.encrypted_secret_id), SecretKey=reveal(settings.encrypted_secret_key),
            Scheme="https", Timeout=60, SignParams=True))

    def _call(self, name, **kwargs):
        try:
            return getattr(self.sdk, name)(Bucket=self.bucket, **kwargs)
        except CosServiceError as error:
            if name in ("head_object", "abort_multipart_upload") and error.get_status_code() == 404:
                return None
            raise HTTPException(503, "对象存储请求失败，请检查连接或权限后重试") from None
        except Exception:
            raise HTTPException(503, "对象存储请求失败，请检查连接或权限后重试") from None

    def put(self, key, stream, content_type="application/octet-stream"):
        return self._call("put_object", Key=key, Body=stream, ContentType=content_type)

    def download(self, key):
        output = TemporaryFile()
        try:
            response = self._call("get_object", Key=key)
            body = response["Body"]
            try:
                for chunk in body.get_stream(chunk_size=1024 * 1024):
                    output.write(chunk)
            finally:
                body.get_raw_stream().close()
            output.seek(0)
            return output
        except Exception:
            output.close()
            raise HTTPException(503, "文件读取失败，请稍后重试") from None

    def head(self, key):
        return self._call("head_object", Key=key)

    def download_range(self, key, start, end):
        response = self._call("get_object", Key=key, Range=f"bytes={start}-{end}")
        return response["Body"].get_raw_stream()

    def delete(self, key):
        self._call("delete_object", Key=key)

    def begin(self, key, content_type):
        return self._call("create_multipart_upload", Key=key, ContentType=content_type)["UploadId"]

    def parts(self, key, upload_id):
        # The 200MiB ceiling and 8MiB parts fit in one COS ListParts page.
        result = self._call("list_parts", Key=key, UploadId=upload_id, MaxParts=1000)
        return result.get("Part", [])

    def complete(self, key, upload_id, parts):
        return self._call("complete_multipart_upload", Key=key, UploadId=upload_id,
            MultipartUpload={"Part": parts})

    def abort(self, key, upload_id):
        self._call("abort_multipart_upload", Key=key, UploadId=upload_id)

    def part_url(self, key, upload_id, number, size):
        try:
            return self.sdk.get_presigned_url(Bucket=self.bucket, Key=key, Method="PUT", Expired=900,
                Params={"uploadId": upload_id, "partNumber": str(number)}, Headers={"Content-Length": str(size)})
        except Exception:
            raise HTTPException(503, "上传链接生成失败，请稍后重试") from None

    def read_url(self, key):
        try:
            return self.sdk.get_presigned_url(Bucket=self.bucket, Key=key, Method="GET", Expired=3600)
        except Exception:
            raise HTTPException(503, "文件读取链接生成失败，请稍后重试") from None

    def iter_objects(self):
        marker = ""
        while True:
            response = self._call("list_objects", Marker=marker, MaxKeys=1000)
            rows = response.get("Contents", [])
            yield from [rows] if isinstance(rows, dict) else rows
            if str(response.get("IsTruncated", "")).lower() != "true":
                return
            marker = response.get("NextMarker") or rows[-1]["Key"]

    def incomplete_uploads(self):
        key_marker, upload_marker = "", ""
        while True:
            response = self._call("list_multipart_uploads", KeyMarker=key_marker,
                UploadIdMarker=upload_marker, MaxUploads=1000)
            rows = response.get("Upload", [])
            yield from [rows] if isinstance(rows, dict) else rows
            if str(response.get("IsTruncated", "")).lower() != "true":
                return
            key_marker, upload_marker = response["NextKeyMarker"], response["NextUploadIdMarker"]
