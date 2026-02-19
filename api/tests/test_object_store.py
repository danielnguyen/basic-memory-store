from __future__ import annotations

import logging

import pytest

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
    def __init__(self):
        self.created = False
        self.raise_head_bucket = None
        self.raise_head_object = None

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

    def generate_presigned_url(self, ClientMethod: str, Params: dict, ExpiresIn: int) -> str:
        return "http://minio:9000/memory-artifacts/path/file.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"


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


def test_rewrite_presigned_url_preserves_query_and_base_path():
    fake = FakeS3()
    client = ObjectStoreClient(
        "http://minio:9000",
        "memory-artifacts",
        "a",
        "b",
        presign_base_url="https://files.example.com/storage",
    )
    client._client = fake

    url = client.create_presigned_get_url("path/file.txt", expires_s=900)

    assert url.startswith("https://files.example.com/storage/memory-artifacts/path/file.txt?")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url


def test_invalid_presign_base_url_logs_warning_and_disables_rewrite(caplog):
    fake = FakeS3()
    client = ObjectStoreClient(
        "http://minio:9000",
        "memory-artifacts",
        "a",
        "b",
        presign_base_url="not-a-url",
    )
    client._client = fake

    with caplog.at_level(logging.WARNING):
        url = client.create_presigned_get_url("path/file.txt", expires_s=900)

    assert url.startswith("http://minio:9000/")
    assert "rewrite disabled" in caplog.text
