"""S3 calls used by a backup run. This module never deletes objects."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from immich_archive.config import Config
from immich_archive.decide import originals_prefix, relative_from_object_key

MISSING = {"404", "NoSuchKey", "NotFound"}


def make_client(config: Config):
    kwargs: dict = {
        "region_name": config.region,
        "aws_access_key_id": config.access_key,
        "aws_secret_access_key": config.secret_key,
    }
    boto_config_args: dict = {
        "request_checksum_calculation": "when_required",
        "response_checksum_validation": "when_required",
    }
    if config.endpoint_url:
        kwargs["endpoint_url"] = config.endpoint_url
        boto_config_args["s3"] = {"addressing_style": "path"}
    kwargs["config"] = BotoConfig(**boto_config_args)
    return boto3.client("s3", **kwargs)


def preflight(client, bucket: str) -> None:
    """Raise ClientError when the bucket is missing or access is denied."""
    client.head_bucket(Bucket=bucket)


def is_missing(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in MISSING or status == 404


def head_sha256(client, bucket: str, key: str) -> str | None:
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if is_missing(error):
            return None
        raise
    metadata = response.get("Metadata") or {}
    value = metadata.get("sha256") or metadata.get("Sha256")
    if not value:
        return None
    return str(value)


def _checksum(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def upload_bytes(
    client,
    *,
    bucket: str,
    key: str,
    body: bytes,
    storage_class: str,
    metadata: dict[str, str],
) -> None:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        StorageClass=storage_class,
        Metadata=metadata,
        ChecksumAlgorithm="SHA256",
        ChecksumSHA256=_checksum(body),
    )


def upload_file(
    client,
    *,
    bucket: str,
    key: str,
    path: Path,
    storage_class: str,
    sha256: str,
    multipart_threshold: int,
) -> str:
    """Upload `path`. Returns the lowercase SHA-256 of the bytes that were sent.

    A multipart upload that fails is aborted. Nothing in this module deletes an object.
    """
    metadata = {"sha256": sha256}
    size = path.stat().st_size
    if size < multipart_threshold:
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        metadata["sha256"] = actual
        upload_bytes(
            client,
            bucket=bucket,
            key=key,
            body=data,
            storage_class=storage_class,
            metadata=metadata,
        )
        return actual

    created = client.create_multipart_upload(
        Bucket=bucket,
        Key=key,
        StorageClass=storage_class,
        Metadata=metadata,
        ChecksumAlgorithm="SHA256",
    )
    upload_id = created["UploadId"]
    try:
        parts = []
        hasher = hashlib.sha256()
        part_number = 1
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                response = client.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                    ChecksumAlgorithm="SHA256",
                    ChecksumSHA256=_checksum(chunk),
                )
                part = {"PartNumber": part_number, "ETag": response["ETag"]}
                if response.get("ChecksumSHA256"):
                    part["ChecksumSHA256"] = response["ChecksumSHA256"]
                parts.append(part)
                part_number += 1
        actual = hasher.hexdigest()
        if not parts:
            raise RuntimeError("refusing to complete a multipart upload with no parts")
        if actual != sha256:
            raise RuntimeError("file changed while it was being uploaded")
        client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise
    return actual


def list_archived(client, bucket: str, prefix: str) -> list[tuple[str, str, int, str]]:
    """Return (relative_path, key, size, sha256) for originals that have a checksum."""
    found: list[tuple[str, str, int, str]] = []
    token = None
    while True:
        kwargs: dict = {"Bucket": bucket, "Prefix": originals_prefix(prefix)}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        for item in response.get("Contents") or []:
            key = item["Key"]
            relative = relative_from_object_key(prefix, key)
            if relative is None:
                continue
            checksum = head_sha256(client, bucket, key)
            if checksum is None:
                continue
            found.append((relative, key, int(item["Size"]), checksum))
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
    return found
