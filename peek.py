import json
from pathlib import Path

import requests

config = json.loads(Path("config.json").read_text())
platform = next(p for p in config["platforms"] if p["title"] == "ibnmalek")

# 1) Login
login = requests.post(
    f"{platform['supabase_url']}/auth/v1/token?grant_type=password",
    headers={
        "apikey": platform["supabase_anon_key"],
        "Content-Type": "application/json",
    },
    json={"email": config["email"], "password": config["password"]},
    timeout=30,
)
login.raise_for_status()
token = login.json()["access_token"]
print("Login OK\n")

# 2) Try to list every table/view the account can see, via the OpenAPI root.
#    Some Supabase projects only accept "apikey" here (no Authorization header).
print("=== Trying OpenAPI schema discovery ===")

found_tables = False

for label, headers in [
    ("apikey + Authorization", {
        "apikey": platform["supabase_anon_key"],
        "Authorization": f"Bearer {token}",
    }),
    ("apikey only", {
        "apikey": platform["supabase_anon_key"],
    }),
]:
    response = requests.get(
        f"{platform['supabase_url']}/rest/v1/",
        headers=headers,
        timeout=30,
    )
    print(f"[{label}] HTTP {response.status_code}")

    if response.status_code == 200:
        try:
            spec = response.json()
            paths = spec.get("paths", {})
        except ValueError:
            paths = {}

        if paths:
            found_tables = True
            print(f"\nعدد الجداول/الـ views المسموح بيها: {len(paths)}\n")
            for name in sorted(paths):
                print(name.lstrip("/"))
            break

if not found_tables:
    # 3) Fallback: probe likely payment-related table names directly.
    print("\n=== OpenAPI discovery unavailable. Probing table names directly ===\n")

    candidates = [
        "payments",
        "invoices",
        "invoice",
        "payment",
        "payment_requests",
        "payment_request",
        "orders",
        "order",
        "transactions",
        "view_payments_with_profiles",
        "view_payments",
        "analytics_payments",  # already confirmed working, included as a sanity check
    ]

    for table_name in candidates:
        r = requests.get(
            f"{platform['supabase_url']}/rest/v1/{table_name}",
            headers={
                "apikey": platform["supabase_anon_key"],
                "Authorization": f"Bearer {token}",
            },
            params={"limit": 1},
            timeout=30,
        )
        print(f"{table_name:35s} -> HTTP {r.status_code}")

        if r.status_code == 200:
            try:
                rows = r.json()
            except ValueError:
                rows = None

            if isinstance(rows, list) and rows:
                print(f"    columns: {', '.join(rows[0].keys())}")
            elif isinstance(rows, list):
                print("    (table exists and is accessible, but returned 0 rows)")
