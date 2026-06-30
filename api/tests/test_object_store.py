from __future__ import annotations

import pytest
from io import BytesIO

botocore_exceptions = pytest.importorskip("botocore.exceptions")
ClientError = botocore_exceptions.ClientError

from storage.object_store import ObjectStoreClient


def _client_error(code: str, status_code: int = 400) -> ClientError:
    return ClientError(
        error_response={
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        operation_name="test",
    )


class FakeS3:
    def __init__(self, base_url: str = "http://minio:9000"):
        self.base_url = base_url.rstrip("/")
        self.created = False
        self.raise_head_bucket = None
        self.raise_head_object = None
        self.raise_get_object = None
        self.object_bytes = b"hello"
        self.presign_calls = []

    def head_bucket(self, Bucket: str):
        if self.raise_head_bucket:
            raise self.raise_head_bucket
        return {"ok": True}

    def create_bucket(self, Bucket: str):
        self.created = True
        return {"ok": True}

    def head_object(self, Bucket: str, Key: str):
        if self.raise_head_object:
            raise self.raise_head_object
        return {"ContentLength": 10, "ContentType": "text/plain"}

    def get_object(self, Bucket: str, Key: str, Range: str):
        if self.raise_get_object:
            raise self.raise_get_object
        return {"Body": BytesIO(self.object_bytes)}

    def generate_presigned_url(self, ClientMethod: str, Params: dict, ExpiresIn: int) -> str:
        self.presign_calls.append({"method": ClientMethod, "params": Params, "expires": ExpiresIn})
        return f"{self.base_url}/memory-artifacts/path/file.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"


def test_ensure_bucket_creates_only_when_missing():
    fake = FakeS3()
    fake.raise_head_bucket = _client_error("404", 404)
    client = ObjectStoreClient("http://minio:9000", "memory-artifacts", "a", "b")
    client._client = fake

    client.ensure_bucket()

    assert fake.created is True


def test_ensure_bucket_raises_on_auth_error():
    fake = FakeS3()
    fake.raise_head_bucket = _client_error("AccessDenied", 403)
    client = ObjectStoreClient("http://minio:9000", "memory-artifacts", "a", "b")
    client._client = fake

    with pytest.raises(RuntimeError, match="auth failure"):
        client.ensure_bucket()
    assert fake.created is False


def test_head_object_returns_none_on_missing():
    fake = FakeS3()
    fake.raise_head_object = _client_error("NotFound", 404)
    client = ObjectStoreClient("http://minio:9000", "memory-artifacts", "a", "b")
    client._client = fake

    assert client.head_object("missing.txt") is None


def test_head_object_raises_on_non_missing_error():
    fake = FakeS3()
    fake.raise_head_object = _client_error("AccessDenied", 403)
    client = ObjectStoreClient("http://minio:9000", "memory-artifacts", "a", "b")
    client._client = fake

    with pytest.raises(RuntimeError, match="head_object failed"):
        client.head_object("denied.txt")


def test_read_object_bytes_is_bounded():
    fake = FakeS3()
    fake.object_bytes = b"abcdef"
    client = ObjectStoreClient("http://minio:9000", "memory-artifacts", "a", "b")
    client._client = fake

    with pytest.raises(RuntimeError, match="exceeds configured"):
        client.read_object_bytes("large.txt", max_bytes=5)


def test_read_object_bytes_missing_is_bounded():
    fake = FakeS3()
    fake.raise_get_object = _client_error("NoSuchKey", 404)
    client = ObjectStoreClient("http://minio:9000", "memory-artifacts", "a", "b")
    client._client = fake

    with pytest.raises(RuntimeError, match="object is missing"):
        client.read_object_bytes("missing.txt", max_bytes=5)


def test_presign_uses_client_visible_endpoint_without_rewriting(monkeypatch):
    ops = FakeS3()
    presign = FakeS3("http://client-minio:9000")
    client = ObjectStoreClient(
        "http://minio:9000",
        "memory-artifacts",
        "a",
        "b",
        presign_base_url="http://client-minio:9000",
    )
    built = []

    def fake_build(endpoint):
        built.append(endpoint)
        return presign if endpoint == "http://client-minio:9000" else ops

    monkeypatch.setattr(client, "_build_client", fake_build)

    url = client.create_presigned_get_url("path/file.txt", expires_s=900)

    assert built == ["http://client-minio:9000"]
    assert url.startswith("http://client-minio:9000/memory-artifacts/path/file.txt?")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url


def test_presign_uses_operations_endpoint_when_no_separate_endpoint(monkeypatch):
    fake = FakeS3()
    client = ObjectStoreClient(
        "http://minio:9000",
        "memory-artifacts",
        "a",
        "b",
    )
    built = []

    def fake_build(endpoint):
        built.append(endpoint)
        return fake

    monkeypatch.setattr(client, "_build_client", fake_build)

    url = client.create_presigned_get_url("path/file.txt", expires_s=900)

    assert built == ["http://minio:9000"]
    assert url.startswith("http://minio:9000/")


def test_invalid_presign_endpoint_fails_boundedly():
    client = ObjectStoreClient(
        "http://minio:9000",
        "memory-artifacts",
        "a",
        "b",
        presign_base_url="not-a-url",
    )

    with pytest.raises(RuntimeError, match="Invalid object store presign endpoint configuration"):
        client.create_presigned_get_url("path/file.txt", expires_s=900)


def test_presigned_put_includes_exact_content_type_when_enabled():
    fake = FakeS3()
    client = ObjectStoreClient("http://minio:9000", "memory-artifacts", "a", "b")
    client._presign_client = fake

    client.create_presigned_put_url("path/file.txt", content_type="text/plain", expires_s=900)

    assert fake.presign_calls[0]["params"]["ContentType"] == "text/plain"


def test_presigned_put_omits_content_type_when_disabled():
    fake = FakeS3()
    client = ObjectStoreClient(
        "http://minio:9000",
        "memory-artifacts",
        "a",
        "b",
        include_content_type_in_put_signature=False,
    )
    client._presign_client = fake

    client.create_presigned_put_url("path/file.txt", content_type="text/plain", expires_s=900)

    assert "ContentType" not in fake.presign_calls[0]["params"]
