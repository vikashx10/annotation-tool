import os
import boto3
from botocore.exceptions import ClientError

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def get_s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),  # supports MinIO / custom S3
    )


def list_s3_images(bucket, prefix=""):
    """Yield S3 keys for all image objects under bucket/prefix."""
    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    normalized_prefix = prefix.rstrip("/") + "/" if prefix else ""

    for page in paginator.paginate(Bucket=bucket, Prefix=normalized_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            ext = os.path.splitext(key)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                yield key


def generate_presigned_url(bucket, key, expiry=3600):
    """Return a presigned GET URL for the given S3 object."""
    s3 = get_s3_client()
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry,
    )


def get_object_bytes(bucket, key):
    """Download and return raw bytes of an S3 object."""
    s3 = get_s3_client()
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def put_object(bucket, key, data, content_type="application/octet-stream"):
    """Upload bytes/file-like object to S3."""
    s3 = get_s3_client()
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)


def object_exists(bucket, key):
    """Return True if the S3 object exists."""
    s3 = get_s3_client()
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def list_s3_folders(bucket, prefix=""):
    """Return immediate sub-folders (common prefixes) under bucket/prefix.

    Uses delimiter='/' so only the first nesting level is returned,
    allowing step-by-step browsing.
    """
    s3 = get_s3_client()
    normalized = prefix.rstrip("/") + "/" if prefix else ""
    resp = s3.list_objects_v2(
        Bucket=bucket, Prefix=normalized, Delimiter="/", MaxKeys=1000
    )
    folders = [cp["Prefix"] for cp in resp.get("CommonPrefixes", [])]
    # Also check if there are direct image objects at this level
    has_images = any(
        os.path.splitext(obj["Key"])[1].lower() in IMAGE_EXTENSIONS
        for obj in resp.get("Contents", [])
    )
    return folders, has_images


def validate_bucket_access(bucket, prefix=""):
    """Raise ValueError if bucket/prefix is not accessible."""
    s3 = get_s3_client()
    normalized_prefix = prefix.rstrip("/") + "/" if prefix else ""
    try:
        s3.list_objects_v2(Bucket=bucket, Prefix=normalized_prefix, MaxKeys=1)
    except ClientError as e:
        raise ValueError(f"S3 access error: {e.response['Error']['Message']}")
