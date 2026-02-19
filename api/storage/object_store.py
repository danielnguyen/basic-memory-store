from __future__ import annotations

from dataclasses import dataclass
import logging
from urllib.parse import urlparse, urlunparse
from prometheus_client import Counter


logger = logging.getLogger(__name__)
object_store_errors_total = Counter(
    "object_store_errors_total",
    "Count of object store operation failures",
    ["operation", "error_class"],
)


@dataclass
class ObjectMetadata:
    size: int
    content_type: str | None


class ObjectStoreClient:
    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        presign_base_url: str | None = None,
        include_content_type_in_put_signature: bool = True,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.presign_base_url = presign_base_url
        self.include_content_type_in_put_signature = include_content_type_in_put_signature
        self._client = None
        self._presign_base_is_valid: bool | None = None

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise RuntimeError("boto3 is required for object-store integration") from e

        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(signature_version="s3v4"),
        )
        return self._client

    def _rewrite_presigned_url(self, url: str) -> str:
        if not self.presign_base_url:
            return url
        src = urlparse(url)
        dst = urlparse(self.presign_base_url)
        if not dst.scheme or not dst.netloc:
            if self._presign_base_is_valid is not False:
                logger.warning("Invalid OBJECT_STORE_PRESIGN_BASE_URL=%r; presigned URL rewrite disabled", self.presign_base_url)
                self._presign_base_is_valid = False
            return url

        self._presign_base_is_valid = True
        base_path = dst.path.rstrip("/")
        rewritten_path = f"{base_path}{src.path}" if base_path else src.path
        return urlunparse((dst.scheme, dst.netloc, rewritten_path, src.params, src.query, src.fragment))

    @staticmethod
    def _is_missing_error(code: str | None, status_code: int | None) -> bool:
        code_norm = (code or "").strip()
        return code_norm in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"} or status_code == 404

    @staticmethod
    def _is_auth_error(code: str | None) -> bool:
        code_norm = (code or "").strip()
        return code_norm in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}

    def ensure_bucket(self) -> None:
        client = self._get_client()
        from botocore.exceptions import ClientError, EndpointConnectionError

        try:
            client.head_bucket(Bucket=self.bucket)
            return
        except ClientError as e:
            err = e.response.get("Error", {}) if isinstance(getattr(e, "response", None), dict) else {}
            code = err.get("Code")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(getattr(e, "response", None), dict) else None
            if self._is_missing_error(code, status):
                try:
                    client.create_bucket(Bucket=self.bucket)
                    return
                except Exception as ce:
                    object_store_errors_total.labels(operation="ensure_bucket", error_class="other").inc()
                    raise RuntimeError(f"Object store bucket create failed for '{self.bucket}': {ce}") from ce
            if self._is_auth_error(code):
                object_store_errors_total.labels(operation="ensure_bucket", error_class="client_error").inc()
                raise RuntimeError(f"Object store auth failure while checking bucket '{self.bucket}': {code}") from e
            object_store_errors_total.labels(operation="ensure_bucket", error_class="client_error").inc()
            raise RuntimeError(f"Object store head_bucket failed for '{self.bucket}': {code or 'unknown'}") from e
        except EndpointConnectionError as e:
            object_store_errors_total.labels(operation="ensure_bucket", error_class="other").inc()
            raise RuntimeError(f"Object store endpoint unreachable while checking bucket '{self.bucket}'") from e

    def create_presigned_put_url(self, key: str, content_type: str, expires_s: int) -> str:
        """
        If ContentType is included in signing, clients MUST send the exact same
        Content-Type header when uploading via the presigned URL.
        """
        client = self._get_client()
        params = {"Bucket": self.bucket, "Key": key}
        if self.include_content_type_in_put_signature:
            params["ContentType"] = content_type
        try:
            url = client.generate_presigned_url(
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=expires_s,
            )
        except Exception as e:
            object_store_errors_total.labels(operation="presign_put", error_class="other").inc()
            raise RuntimeError(f"Failed to generate presigned PUT URL: {e}") from e
        return self._rewrite_presigned_url(url)

    def create_presigned_get_url(self, key: str, expires_s: int) -> str:
        client = self._get_client()
        try:
            url = client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_s,
            )
        except Exception as e:
            object_store_errors_total.labels(operation="presign_get", error_class="other").inc()
            raise RuntimeError(f"Failed to generate presigned GET URL: {e}") from e
        return self._rewrite_presigned_url(url)

    def head_object(self, key: str) -> ObjectMetadata | None:
        client = self._get_client()
        from botocore.exceptions import ClientError, EndpointConnectionError

        try:
            out = client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            err = e.response.get("Error", {}) if isinstance(getattr(e, "response", None), dict) else {}
            code = err.get("Code")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(getattr(e, "response", None), dict) else None
            if self._is_missing_error(code, status):
                return None
            object_store_errors_total.labels(operation="head_object", error_class="client_error").inc()
            raise RuntimeError(f"Object store head_object failed for key '{key}': {code or 'unknown'}") from e
        except EndpointConnectionError as e:
            object_store_errors_total.labels(operation="head_object", error_class="other").inc()
            raise RuntimeError(f"Object store endpoint unreachable while reading key '{key}'") from e
        return ObjectMetadata(
            size=int(out.get("ContentLength", 0)),
            content_type=out.get("ContentType"),
        )
