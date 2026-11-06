"""OSS V2 adapter. Vendor exceptions and signed URLs must never reach clients/logs."""
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryFile

import alibabacloud_oss_v2 as oss
from fastapi import HTTPException

from .site_auth import reveal


class OssStorage:
    def __init__(self, settings):
        if not settings or not settings.encrypted_secret_id or not settings.encrypted_secret_key:
            raise HTTPException(503, "文件存储尚未配置，请联系站点管理员")
        self.bucket, self.region = settings.bucket, settings.region
        config = oss.config.load_default()
        config.region = self.region
        config.endpoint = f"https://oss-{self.region}.aliyuncs.com"
        config.credentials_provider = oss.credentials.StaticCredentialsProvider(
            reveal(settings.encrypted_secret_id), reveal(settings.encrypted_secret_key))
        self.credentials = config.credentials_provider
        config.connect_timeout, config.readwrite_timeout = 10, 60
        self.sdk = oss.Client(config)

    def _request(self, operation, request):
        try:
            return getattr(self.sdk, operation)(request)
        except oss.exceptions.ServiceError as error:
            if error.status_code == 404 and operation in ("head_object", "abort_multipart_upload"):
                return None
            raise HTTPException(503, "对象存储请求失败，请检查连接或权限后重试") from None
        except Exception:
            raise HTTPException(503, "对象存储请求失败，请检查连接或权限后重试") from None

    def put(self, key, stream, content_type="application/octet-stream"):
        return self._request("put_object", oss.PutObjectRequest(
            bucket=self.bucket, key=key, body=stream, content_type=content_type))

    def download(self, key):
        return self._download(oss.GetObjectRequest(bucket=self.bucket, key=key))

    def _download(self, request):
        output, body = TemporaryFile(), None
        try:
            body = self._request("get_object", request).body
            for chunk in body.iter_bytes(chunk_size=1024 * 1024):
                output.write(chunk)
            output.seek(0)
            return output
        except Exception:
            output.close()
            raise HTTPException(503, "文件读取失败，请稍后重试") from None
        finally:
            if body is not None:
                body.close()

    def download_range(self, key, start, end):
        return self._download(oss.GetObjectRequest(bucket=self.bucket,
            key=key, range_header=f"bytes={start}-{end}"))

    def head(self, key):
        result = self._request("head_object", oss.HeadObjectRequest(bucket=self.bucket, key=key))
        return None if result is None else {"Content-Length": result.content_length,
            "Content-Type": result.content_type, "ETag": result.etag}

    def delete(self, key):
        self._request("delete_object", oss.DeleteObjectRequest(bucket=self.bucket, key=key))

    def begin(self, key, content_type):
        return self._request("initiate_multipart_upload", oss.InitiateMultipartUploadRequest(
            bucket=self.bucket, key=key, content_type=content_type)).upload_id

    def parts(self, key, upload_id):
        marker, parts = 0, []
        while True:
            result = self._request("list_parts", oss.ListPartsRequest(bucket=self.bucket,
                key=key, upload_id=upload_id, max_parts=1000, part_number_marker=marker))
            parts.extend({"PartNumber": p.part_number, "Size": p.size, "ETag": p.etag} for p in result.parts or [])
            if not result.is_truncated:
                return parts
            marker = result.next_part_number_marker

    def upload_part(self, key, upload_id, number, body):
        return self._request("upload_part", oss.UploadPartRequest(bucket=self.bucket, key=key,
            upload_id=upload_id, part_number=number, body=body)).etag

    def complete(self, key, upload_id, parts):
        return self._request("complete_multipart_upload", oss.CompleteMultipartUploadRequest(
            bucket=self.bucket, key=key, upload_id=upload_id,
            complete_multipart_upload=oss.CompleteMultipartUpload(parts=[
                oss.Part(part_number=p["PartNumber"], etag=p["ETag"]) for p in parts])))

    def abort(self, key, upload_id):
        self._request("abort_multipart_upload", oss.AbortMultipartUploadRequest(
            bucket=self.bucket, key=key, upload_id=upload_id))

    def _presign(self, request, seconds):
        try:
            return self.sdk.presign(request, expires=timedelta(seconds=seconds)).url
        except Exception:
            raise HTTPException(503, "文件链接生成失败，请稍后重试") from None

    def part_url(self, key, upload_id, number, size):
        url = self._presign(oss.UploadPartRequest(bucket=self.bucket, key=key,
            upload_id=upload_id, part_number=number, content_length=size), 900)
        # V2 1.4.1 does not propagate Config.additional_headers to SigningContext.
        # Re-sign through the public SDK signer to bind the exact browser body size.
        try:
            request = oss.types.HttpRequest("PUT", url, headers={"Content-Length": str(size)})
            context = oss.types.SigningContext(product="oss", region=self.region, bucket=self.bucket,
                key=key, request=request, credentials=self.credentials.get_credentials(),
                additional_headers={"content-length"})
            context.auth_method_query = True
            context.expiration_time = datetime.now(timezone.utc) + timedelta(seconds=900)
            oss.signer.SignerV4().sign(context)
            return request.url
        except Exception:
            raise HTTPException(503, "上传链接生成失败，请稍后重试") from None

    def read_url(self, key):
        return self._presign(oss.GetObjectRequest(bucket=self.bucket, key=key), 3600)

    def iter_objects(self):
        token = None
        while True:
            result = self._request("list_objects_v2", oss.ListObjectsV2Request(
                bucket=self.bucket, max_keys=1000, continuation_token=token))
            for item in result.contents or []:
                yield {"Key": item.key, "Size": item.size,
                    "LastModified": item.last_modified.isoformat() if item.last_modified else None}
            if not result.is_truncated:
                return
            token = result.next_continuation_token

    def incomplete_uploads(self):
        key_marker = upload_marker = None
        while True:
            result = self._request("list_multipart_uploads", oss.ListMultipartUploadsRequest(
                bucket=self.bucket, max_uploads=1000, key_marker=key_marker, upload_id_marker=upload_marker))
            for item in result.uploads or []:
                yield {"Key": item.key, "UploadId": item.upload_id,
                    "Initiated": item.initiated.isoformat() if item.initiated else None}
            if not result.is_truncated:
                return
            key_marker, upload_marker = result.next_key_marker, result.next_upload_id_marker
