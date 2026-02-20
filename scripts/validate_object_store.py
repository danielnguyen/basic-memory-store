#!/usr/bin/env python3
import os
import sys
import json
import httpx

base = os.environ.get("BASE", "http://127.0.0.1:8000")
api_key = os.environ.get("MEMORY_API_KEY")
owner_id = os.environ.get("OWNER_ID", "daniel")
filename = os.environ.get("FILENAME", "exec-test.txt")
mime = os.environ.get("MIME_TYPE", "text/plain")
content = os.environ.get("CONTENT", "artifact validation via container script\n").encode()

if not api_key:
    print("MEMORY_API_KEY is required", file=sys.stderr)
    sys.exit(1)

headers = {
    "X-API-Key": api_key,
    "Content-Type": "application/json",
}

with httpx.Client(timeout=30) as client:
    # 1) Init upload
    init_resp = client.post(
        f"{base}/v1/artifacts/init",
        headers=headers,
        json={
            "owner_id": owner_id,
            "filename": filename,
            "mime": mime,
            "size": len(content),
        },
    )
    init_resp.raise_for_status()
    init_data = init_resp.json()
    print("init:")
    print(json.dumps(init_data, indent=2))

    artifact_id = init_data["artifact_id"]
    upload_url = init_data["upload_url"]

    # 2) Upload object via presigned PUT
    put_resp = client.put(
        upload_url,
        headers={"Content-Type": mime},
        content=content,
    )
    put_resp.raise_for_status()
    print(f"put status: {put_resp.status_code}")

    # 3) Complete upload
    complete_resp = client.post(
        f"{base}/v1/artifacts/complete",
        headers=headers,
        json={"artifact_id": artifact_id, "status": "completed"},
    )
    complete_resp.raise_for_status()
    complete_data = complete_resp.json()
    print("complete:")
    print(json.dumps(complete_data, indent=2))

    # 4) Fetch metadata + download
    meta_resp = client.get(
        f"{base}/v1/artifacts/{artifact_id}",
        headers={"X-API-Key": api_key},
    )
    meta_resp.raise_for_status()
    meta_data = meta_resp.json()
    print("meta:")
    print(json.dumps(meta_data, indent=2))

    download_url = meta_data["download_url"]
    get_resp = client.get(download_url)
    get_resp.raise_for_status()
    print(f"download status: {get_resp.status_code}, bytes: {len(get_resp.content)}")
    print("download body:", get_resp.text[:120])

print("✅ object-store flow validated from inside container network")
