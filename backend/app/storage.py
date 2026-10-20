import boto3
from botocore.exceptions import ClientError
from time import sleep

from .config import get_settings


def client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=__import__("botocore").config.Config(s3={"addressing_style": "path"}),
    )


def ensure_bucket() -> None:
    settings = get_settings()
    s3 = client()
    last_error = None
    for _ in range(12):
        try:
            s3.head_bucket(Bucket=settings.s3_bucket)
            return
        except ClientError:
            try:
                s3.create_bucket(Bucket=settings.s3_bucket)
                return
            except ClientError as error:
                last_error = error
                sleep(2)
    raise RuntimeError(f"Object storage is unavailable: {last_error}")


def upload_fileobj(fileobj, object_key: str, content_type: str) -> None:
    settings = get_settings()
    client().upload_fileobj(
        fileobj,
        settings.s3_bucket,
        object_key,
        ExtraArgs={"ContentType": content_type},
    )


def download_fileobj(object_key: str):
    settings = get_settings()
    output = __import__("io").BytesIO()
    client().download_fileobj(settings.s3_bucket, object_key, output)
    output.seek(0)
    return output

