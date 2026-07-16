"""Raw payload storage.

Two implementations:

- :class:`FilesystemRawStore` — gzipped JSON on local disk. Great for dev.
- :class:`SpacesRawStore` — DO Spaces (S3-compatible) using aioboto3. Great for
  prod: cheap, unlimited, decouples payload retention from OLTP DB size.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import aioboto3  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional in tests
    aioboto3 = None


def _key_for(request_id: str) -> str:
    now = datetime.now(UTC)
    return f"raw/{now:%Y/%m/%d}/{request_id}.json.gz"


class FilesystemRawStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    async def put(self, request_id: str, payload: dict[str, Any]) -> str:
        key = _key_for(request_id)
        target = self._root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        with gzip.open(target, "wb") as fh:
            fh.write(encoded)
        return key

    async def get(self, object_key: str) -> dict[str, Any]:
        target = self._root / object_key
        with gzip.open(target, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    async def aclose(self) -> None:
        return None


class SpacesRawStore:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region: str,
        access_key: str,
        secret_key: str,
    ) -> None:
        if aioboto3 is None:  # pragma: no cover
            raise RuntimeError("aioboto3 is required for SpacesRawStore")
        if not bucket:
            raise ValueError("bucket is required for SpacesRawStore")
        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._session = aioboto3.Session()

    def _client(self) -> Any:
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )

    async def put(self, request_id: str, payload: dict[str, Any]) -> str:
        key = _key_for(request_id)
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        body = gzip.compress(encoded)
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ContentEncoding="gzip",
            )
        return key

    async def get(self, object_key: str) -> dict[str, Any]:
        async with self._client() as s3:
            resp = await s3.get_object(Bucket=self._bucket, Key=object_key)
            body = await resp["Body"].read()
            decoded = gzip.decompress(body)
            return json.loads(decoded.decode("utf-8"))

    async def aclose(self) -> None:
        return None


def build_raw_store(
    *,
    kind: str,
    filesystem_path: str,
    bucket: str,
    endpoint_url: str,
    region: str,
    access_key: str,
    secret_key: str,
) -> FilesystemRawStore | SpacesRawStore:
    if kind == "filesystem":
        return FilesystemRawStore(filesystem_path)
    if kind == "spaces":
        return SpacesRawStore(
            bucket=bucket,
            endpoint_url=endpoint_url,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
        )
    raise ValueError(f"Unknown raw store kind: {kind!r}")
