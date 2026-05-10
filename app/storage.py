from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import shutil
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Iterable, Protocol

from app.config import get_settings

DEFAULT_CHUNK_SIZE = 1024 * 1024


class StorageError(RuntimeError):
    """Base error for object storage operations."""


class ObjectNotFoundError(StorageError):
    """Raised when a requested storage object is missing."""


class RangeNotSatisfiableError(StorageError):
    """Raised when an HTTP byte range cannot be served."""


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    size: int
    content_type: str | None = None


@dataclass(frozen=True)
class ObjectStream:
    key: str
    size: int
    content_type: str | None
    body: Iterable[bytes]
    range_start: int | None = None
    range_end: int | None = None


class ObjectStorageBackend(Protocol):
    provider: str
    bucket_name: str | None

    def put_object(self, key: str, body: BinaryIO, *, content_type: str | None = None) -> None: ...

    def head_object(self, key: str) -> ObjectMetadata: ...

    def get_object(self, key: str) -> ObjectStream: ...

    def get_object_range(self, key: str, *, start: int, end: int) -> ObjectStream: ...

    def delete_object(self, key: str) -> None: ...


class LocalStorageBackend:
    provider = "local"
    bucket_name = None

    def __init__(self, root: str | Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        self.root = Path(root).resolve()
        self.chunk_size = chunk_size
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for_key(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise StorageError("storage key escapes local storage root")
        return candidate

    def put_object(self, key: str, body: BinaryIO, *, content_type: str | None = None) -> None:
        target = self._path_for_key(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            shutil.copyfileobj(body, handle, self.chunk_size)

    def head_object(self, key: str) -> ObjectMetadata:
        path = self._path_for_key(key)
        if not path.exists() or not path.is_file():
            raise ObjectNotFoundError(key)
        return ObjectMetadata(key=key, size=path.stat().st_size)

    def _iter_file(self, path: Path, *, start: int = 0, remaining: int | None = None) -> Iterable[bytes]:
        with path.open("rb") as handle:
            handle.seek(start)
            bytes_left = remaining
            while bytes_left is None or bytes_left > 0:
                read_size = self.chunk_size if bytes_left is None else min(self.chunk_size, bytes_left)
                chunk = handle.read(read_size)
                if not chunk:
                    break
                if bytes_left is not None:
                    bytes_left -= len(chunk)
                yield chunk

    def get_object(self, key: str) -> ObjectStream:
        metadata = self.head_object(key)
        path = self._path_for_key(key)
        return ObjectStream(key=key, size=metadata.size, content_type=None, body=self._iter_file(path))

    def get_object_range(self, key: str, *, start: int, end: int) -> ObjectStream:
        metadata = self.head_object(key)
        if start < 0 or end < start or start >= metadata.size:
            raise RangeNotSatisfiableError(key)
        end = min(end, metadata.size - 1)
        path = self._path_for_key(key)
        return ObjectStream(
            key=key,
            size=metadata.size,
            content_type=None,
            body=self._iter_file(path, start=start, remaining=end - start + 1),
            range_start=start,
            range_end=end,
        )

    def delete_object(self, key: str) -> None:
        self._path_for_key(key).unlink(missing_ok=True)


class FakeStorageBackend:
    provider = "fake"
    bucket_name = "fake"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = {}

    def put_object(self, key: str, body: BinaryIO, *, content_type: str | None = None) -> None:
        self.objects[key] = (body.read(), content_type)

    def head_object(self, key: str) -> ObjectMetadata:
        try:
            value, content_type = self.objects[key]
        except KeyError as exc:
            raise ObjectNotFoundError(key) from exc
        return ObjectMetadata(key=key, size=len(value), content_type=content_type)

    def get_object(self, key: str) -> ObjectStream:
        metadata = self.head_object(key)
        value, content_type = self.objects[key]
        return ObjectStream(key=key, size=metadata.size, content_type=content_type, body=[value])

    def get_object_range(self, key: str, *, start: int, end: int) -> ObjectStream:
        metadata = self.head_object(key)
        if start < 0 or end < start or start >= metadata.size:
            raise RangeNotSatisfiableError(key)
        end = min(end, metadata.size - 1)
        value, content_type = self.objects[key]
        return ObjectStream(
            key=key,
            size=metadata.size,
            content_type=content_type,
            body=[value[start : end + 1]],
            range_start=start,
            range_end=end,
        )

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


class S3StorageBackend:
    provider = "s3"

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        bucket_name: str,
        region_name: str | None,
        access_key: str | None,
        secret_key: str | None,
        force_path_style: bool,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        if not bucket_name:
            raise StorageError("object storage bucket is required for s3 provider")
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise StorageError("boto3 is required when object_storage_provider=s3") from exc

        self.bucket_name = bucket_name
        self.chunk_size = chunk_size
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region_name or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=Config(s3={"addressing_style": "path" if force_path_style else "auto"}),
        )

    def put_object(self, key: str, body: BinaryIO, *, content_type: str | None = None) -> None:
        extra_args = {"ContentType": content_type} if content_type else None
        self.client.upload_fileobj(body, self.bucket_name, key, ExtraArgs=extra_args or {})

    def head_object(self, key: str) -> ObjectMetadata:
        try:
            response = self.client.head_object(Bucket=self.bucket_name, Key=key)
        except Exception as exc:  # boto3 raises generated ClientError classes.
            if _looks_like_not_found(exc):
                raise ObjectNotFoundError(key) from exc
            raise
        return ObjectMetadata(
            key=key,
            size=int(response.get("ContentLength") or 0),
            content_type=response.get("ContentType"),
        )

    def _iter_streaming_body(self, streaming_body) -> Iterable[bytes]:
        try:
            for chunk in streaming_body.iter_chunks(chunk_size=self.chunk_size):
                if chunk:
                    yield chunk
        finally:
            streaming_body.close()

    def get_object(self, key: str) -> ObjectStream:
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=key)
        except Exception as exc:
            if _looks_like_not_found(exc):
                raise ObjectNotFoundError(key) from exc
            raise
        size = int(response.get("ContentLength") or 0)
        return ObjectStream(
            key=key,
            size=size,
            content_type=response.get("ContentType"),
            body=self._iter_streaming_body(response["Body"]),
        )

    def get_object_range(self, key: str, *, start: int, end: int) -> ObjectStream:
        if start < 0 or end < start:
            raise RangeNotSatisfiableError(key)
        total_size = self.head_object(key).size
        if start >= total_size:
            raise RangeNotSatisfiableError(key)
        end = min(end, total_size - 1)
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=key, Range=f"bytes={start}-{end}")
        except Exception as exc:
            if _looks_like_not_found(exc):
                raise ObjectNotFoundError(key) from exc
            raise
        return ObjectStream(
            key=key,
            size=total_size,
            content_type=response.get("ContentType"),
            body=self._iter_streaming_body(response["Body"]),
            range_start=start,
            range_end=end,
        )

    def delete_object(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket_name, Key=key)


def _looks_like_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status_code == 404


def spool_limited_upload(upload, *, max_bytes: int, chunk_size: int = DEFAULT_CHUNK_SIZE) -> tuple[SpooledTemporaryFile, int]:
    file_size = 0
    spooled = SpooledTemporaryFile(max_size=max_bytes)
    try:
        while True:
            chunk = upload.file.read(chunk_size)
            if not chunk:
                break
            file_size += len(chunk)
            if file_size > max_bytes:
                spooled.close()
                raise ValueError("upload exceeds max_bytes")
            spooled.write(chunk)
        spooled.seek(0)
        return spooled, file_size
    finally:
        upload.file.close()


def parse_http_range(range_header: str | None, *, object_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    if object_size <= 0:
        raise RangeNotSatisfiableError("empty object")
    if not range_header.startswith("bytes="):
        raise RangeNotSatisfiableError("unsupported range unit")
    range_spec = range_header.removeprefix("bytes=").strip()
    if "," in range_spec or "-" not in range_spec:
        raise RangeNotSatisfiableError("multiple or malformed ranges are not supported")
    start_text, end_text = range_spec.split("-", 1)
    try:
        if start_text == "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError
            start = max(object_size - suffix_length, 0)
            end = object_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else object_size - 1
    except ValueError as exc:
        raise RangeNotSatisfiableError("malformed byte range") from exc
    if start < 0 or end < start or start >= object_size:
        raise RangeNotSatisfiableError("unsatisfiable byte range")
    return start, min(end, object_size - 1)


@lru_cache(maxsize=1)
def get_storage_backend() -> ObjectStorageBackend:
    settings = get_settings()
    provider = settings.object_storage_provider.lower()
    if provider == "local":
        return LocalStorageBackend(settings.object_storage_local_dir, chunk_size=settings.object_storage_proxy_chunk_size_bytes)
    if provider == "fake":
        return FakeStorageBackend()
    if provider == "s3":
        return S3StorageBackend(
            endpoint_url=settings.object_storage_endpoint,
            bucket_name=settings.object_storage_bucket,
            region_name=settings.object_storage_region,
            access_key=settings.object_storage_access_key,
            secret_key=settings.object_storage_secret_key,
            force_path_style=settings.object_storage_force_path_style,
            chunk_size=settings.object_storage_proxy_chunk_size_bytes,
        )
    raise StorageError(f"unsupported object storage provider: {settings.object_storage_provider}")
