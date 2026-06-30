from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import httpx


def _assert_no_sensitive_text(value: object, *, content: bytes) -> None:
    text = str(value)
    assert "X-Amz-" not in text
    assert "minioadmin" not in text
    assert content.decode("utf-8") not in text


def main() -> None:
    base = os.environ.get("BASE", "http://127.0.0.1:8000")
    api_key = os.environ["MEMORY_API_KEY"]
    owner_id = os.environ.get("OWNER_ID", "smoke-owner")
    client_id = os.environ.get("CLIENT_ID", "smoke-client")
    expected_upload_host = os.environ.get("EXPECT_UPLOAD_HOST", "minio-client:9000")
    content = b"artifact smoke uploaded text alpha beta gamma provenance\n"
    auth = {"X-API-Key": api_key}
    json_auth = {**auth, "Content-Type": "application/json"}

    with httpx.Client(timeout=60) as client:
        unauth = client.post(
            f"{base}/v1/artifacts/init",
            json={"owner_id": owner_id, "filename": "denied.txt", "mime": "text/plain", "size": 5},
        )
        assert unauth.status_code in {401, 403}

        convo = client.post(
            f"{base}/v1/conversations",
            headers=json_auth,
            json={"owner_id": owner_id, "client_id": client_id, "title": "artifact smoke"},
        )
        convo.raise_for_status()
        conversation_id = convo.json()["conversation_id"]

        missing = client.post(
            f"{base}/v1/artifacts/init",
            headers=json_auth,
            json={
                "owner_id": owner_id,
                "client_id": client_id,
                "conversation_id": conversation_id,
                "filename": "missing.txt",
                "mime": "text/plain",
                "size": len(content),
            },
        )
        missing.raise_for_status()
        missing_complete = client.post(
            f"{base}/v1/artifacts/complete",
            headers=json_auth,
            json={"artifact_id": missing.json()["artifact_id"], "owner_id": owner_id, "status": "completed"},
        )
        assert missing_complete.status_code == 409
        _assert_no_sensitive_text(missing_complete.text, content=content)

        mismatch = client.post(
            f"{base}/v1/artifacts/init",
            headers=json_auth,
            json={
                "owner_id": owner_id,
                "client_id": client_id,
                "conversation_id": conversation_id,
                "filename": "mismatch.txt",
                "mime": "text/plain",
                "size": len(content) + 5,
            },
        )
        mismatch.raise_for_status()
        mismatch_put = client.put(mismatch.json()["upload_url"], headers={"Content-Type": "text/plain"}, content=content)
        mismatch_put.raise_for_status()
        mismatch_complete = client.post(
            f"{base}/v1/artifacts/complete",
            headers=json_auth,
            json={"artifact_id": mismatch.json()["artifact_id"], "owner_id": owner_id, "status": "completed"},
        )
        assert mismatch_complete.status_code == 409
        _assert_no_sensitive_text(mismatch_complete.text, content=content)

        init = client.post(
            f"{base}/v1/artifacts/init",
            headers=json_auth,
            json={
                "owner_id": owner_id,
                "client_id": client_id,
                "conversation_id": conversation_id,
                "filename": "smoke artifact.txt",
                "mime": "text/plain",
                "size": len(content),
                "source_surface": "smoke",
            },
        )
        init.raise_for_status()
        init_data = init.json()
        upload_url = init_data["upload_url"]
        assert urlparse(upload_url).netloc == expected_upload_host
        assert upload_url == init_data["upload_url"]

        put = client.put(upload_url, headers={"Content-Type": "text/plain"}, content=content)
        put.raise_for_status()

        artifact_id = init_data["artifact_id"]
        owner_mismatch = client.post(
            f"{base}/v1/artifacts/complete",
            headers=json_auth,
            json={"artifact_id": artifact_id, "owner_id": "other-owner", "status": "completed"},
        )
        assert owner_mismatch.status_code == 404

        complete = client.post(
            f"{base}/v1/artifacts/complete",
            headers=json_auth,
            json={"artifact_id": artifact_id, "owner_id": owner_id, "status": "completed"},
        )
        complete.raise_for_status()
        download_url = complete.json()["download_url"]
        assert urlparse(download_url).netloc == expected_upload_host

        downloaded = client.get(download_url)
        downloaded.raise_for_status()
        assert downloaded.content == content

        request_id = f"artifact-smoke-{uuid4()}"
        retrieval = client.post(
            f"{base}/v2/conversations/{conversation_id}/retrieve",
            headers={**json_auth, "X-Request-ID": request_id},
            json={
                "request_id": request_id,
                "owner_id": owner_id,
                "query": "alpha beta provenance",
                "include_artifacts": True,
                "retrieval": {"k": 3, "min_score": 0.0, "scope": "conversation"},
            },
        )
        retrieval.raise_for_status()
        body = retrieval.json()
        refs = body["bundle"]["artifact_refs"]
        assert refs
        ref = refs[0]
        assert ref["artifact_id"] == artifact_id
        assert "alpha beta" in ref["snippet"]
        assert len(ref["snippet"]) <= 120
        assert ref["source_ref"]["ref_type"] == "derived_text"
        assert ref["provenance"]["source_refs"][0]["ref_id"] == artifact_id
        assert body["request_id"] == request_id
        assert body["conversation_id"] == conversation_id
        _assert_no_sensitive_text(body["diagnostics"], content=content)

    print("artifact storage smoke passed")


if __name__ == "__main__":
    main()
