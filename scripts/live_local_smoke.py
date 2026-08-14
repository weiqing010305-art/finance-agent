from __future__ import annotations

import os
import re
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx


def main() -> None:
    email = os.environ["FINSCOPE_SMOKE_EMAIL"]
    password = os.environ["FINSCOPE_SMOKE_PASSWORD"]
    tenant_id = os.environ["FINSCOPE_SMOKE_TENANT"]
    with httpx.Client(base_url="https://localhost:8443", verify=False, trust_env=False, timeout=20) as client:
        login = client.post("/api/auth/login", json={
            "email": email, "password": password, "tenant_id": tenant_id,
        })
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post("/api/research", headers={
            **headers, "Idempotency-Key": f"live-smoke-{uuid4()}",
        }, json={
            "company": "腾讯控股", "symbol": "0700", "market": "HK",
            "question": "验证本地持久化研究链条是否正常", "depth": "quick", "budget_limit": 20,
        })
        created.raise_for_status(); run_id = created.json()["run_id"]
        task = None
        for _ in range(40):
            task = client.get(f"/api/research/{run_id}", headers=headers).json()
            if task["status"] in {"completed", "failed", "paused"}:
                break
            time.sleep(0.25)
        if task is None or task["status"] != "completed" or not task.get("report"):
            raise RuntimeError(f"research smoke did not complete: {task}")

        body = b"FinScope private object smoke"
        slot_response = client.post("/api/objects/upload-slots", headers=headers, json={
            "content_type": "text/plain", "size": len(body),
        })
        slot_response.raise_for_status(); slot = slot_response.json()
        upload = httpx.post(
            slot["upload_url"], data=slot["upload_fields"],
            files={"file": ("smoke.txt", body, "text/plain")},
            verify=False, trust_env=False, timeout=20,
        )
        upload.raise_for_status()
        verified = client.post(f"/api/objects/{slot['object_id']}/verify", headers=headers)
        verified.raise_for_status()
        if verified.json()["status"] != "ready":
            raise RuntimeError("object promotion did not reach ready")
        download = client.get(f"/api/objects/{slot['object_id']}/download", headers=headers)
        download.raise_for_status()
        fetched = httpx.get(download.json()["download_url"], verify=False, trust_env=False, timeout=20)
        fetched.raise_for_status()
        if fetched.content != body:
            raise RuntimeError("downloaded object does not match uploaded bytes")

        invitee = f"invitee-{uuid4().hex[:10]}@example.com"
        invitation = client.post("/api/auth/invitations", headers=headers, json={
            "email": invitee, "role": "viewer", "expires_in_hours": 1,
        })
        invitation.raise_for_status()
        if "token" in invitation.json():
            raise RuntimeError("formal invitation response exposed the raw token")
        detail = None
        with httpx.Client(base_url="http://127.0.0.1:8025", trust_env=False, timeout=10) as mailbox:
            for _ in range(30):
                listing = mailbox.get("/api/v1/messages").json()
                message = next(
                    (item for item in listing.get("messages", []) if invitee in str(item)), None,
                )
                if message:
                    message_id = message.get("ID") or message.get("id")
                    detail = mailbox.get(f"/api/v1/message/{message_id}").json()
                    break
                time.sleep(0.2)
        if detail is None:
            raise RuntimeError("Mailpit did not receive the invitation")
        text_body = str(detail.get("Text") or detail.get("text") or detail)
        match = re.search(r"https://localhost:8443/invitation\.html\?[^\s<]+", text_body)
        if match is None:
            raise RuntimeError("invitation email does not contain the onboarding page")
        invitation_url = match.group(0).replace("&amp;", "&")
        query = parse_qs(urlsplit(invitation_url).query)
        tenant, token = query["tenant_id"][0], query["token"][0]
        page = client.get(invitation_url)
        page.raise_for_status()
        if "invitation.js" not in page.text:
            raise RuntimeError("invitation page is not served")
        invitee_password = f"Local-invite-{uuid4().hex}!"
        with httpx.Client(base_url="https://localhost:8443", verify=False, trust_env=False, timeout=20) as invitee_client:
            accepted = invitee_client.post("/api/auth/invitations/accept", json={
                "tenant_id": tenant, "token": token, "password": invitee_password,
            })
            accepted.raise_for_status()
            relogin = invitee_client.post("/api/auth/login", json={
                "tenant_id": tenant, "email": invitee, "password": invitee_password,
            })
            relogin.raise_for_status()
    print(
        f"live_local_smoke_passed run={run_id} object={slot['object_id']} "
        f"invitation={invitation.json()['invitation_id']}"
    )


if __name__ == "__main__":
    main()
