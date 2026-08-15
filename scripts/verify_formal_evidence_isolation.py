from __future__ import annotations

import argparse
import os

import httpx


def _login(client: httpx.Client, base_url: str, email: str, password: str, tenant_id: str) -> str:
    response = client.post(f"{base_url}/api/auth/login", json={
        "email": email, "password": password, "tenant_id": tenant_id,
    })
    response.raise_for_status()
    return response.json()["access_token"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify two-account end-to-end API isolation of research runs and evidence",
    )
    parser.add_argument("--base-url", default=os.getenv("FINSCOPE_BASE_URL", "https://localhost:8443"))
    parser.add_argument("--verify-ssl", action="store_true", default=False)
    args = parser.parse_args()

    a_email = os.getenv("FINSCOPE_A_EMAIL")
    a_password = os.getenv("FINSCOPE_A_PASSWORD")
    a_tenant = os.getenv("FINSCOPE_A_TENANT")
    b_email = os.getenv("FINSCOPE_B_EMAIL")
    b_password = os.getenv("FINSCOPE_B_PASSWORD")
    b_tenant = os.getenv("FINSCOPE_B_TENANT")
    if not all((a_email, a_password, a_tenant, b_email, b_password, b_tenant)):
        raise SystemExit(
            "FINSCOPE_A_EMAIL/PASSWORD/TENANT and FINSCOPE_B_EMAIL/PASSWORD/TENANT are required"
        )

    with httpx.Client(verify=args.verify_ssl, timeout=30.0) as client:
        token_a = _login(client, args.base_url, a_email, a_password, a_tenant)
        token_b = _login(client, args.base_url, b_email, b_password, b_tenant)
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        created = client.post(f"{args.base_url}/api/research", headers={
            **headers_a, "Idempotency-Key": "evidence-isolation-demo-0001",
        }, json={
            "company": "腾讯控股", "symbol": "0700", "market": "HK",
            "question": "双账号隔离演示：验证跨租户研究证据不可见", "depth": "quick",
            "budget_limit": 2,
        })
        created.raise_for_status()
        run_id = created.json()["run_id"]

        own = client.get(f"{args.base_url}/api/research/{run_id}/evidence", headers=headers_a)
        other = client.get(f"{args.base_url}/api/research/{run_id}/evidence", headers=headers_b)
        own_run = client.get(f"{args.base_url}/api/research/{run_id}", headers=headers_a)
        other_run = client.get(f"{args.base_url}/api/research/{run_id}", headers=headers_b)

    if own.status_code != 200 or own_run.status_code != 200:
        raise RuntimeError(f"owner cannot read own run/evidence: run={own_run.status_code} evidence={own.status_code}")
    if other.status_code != 404 or other_run.status_code != 404:
        raise RuntimeError(
            f"cross-tenant isolation failed: run={other_run.status_code} evidence={other.status_code}"
        )
    print(
        "formal_evidence_isolation_passed "
        f"owner_run={own_run.status_code} owner_evidence={own.status_code} "
        f"other_run={other_run.status_code} other_evidence={other.status_code}"
    )


if __name__ == "__main__":
    main()
