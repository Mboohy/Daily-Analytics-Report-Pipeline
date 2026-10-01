import json
from pathlib import Path

import requests

config = json.loads(Path("config.json").read_text())
platform = next(p for p in config["platforms"] if p["title"] == "ibnmalek")

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

response = requests.get(
    f"{platform['supabase_url']}/rest/v1/",
    headers={
        "apikey": platform["supabase_anon_key"],
        "Authorization": f"Bearer {token}",
    },
    timeout=30,
)

print("HTTP", response.status_code)
spec = response.json()
paths = spec.get("paths", {})

print(f"\nعدد الجداول/الـ views المسموح بيها: {len(paths)}\n")
for name in sorted(paths):
    print(name.lstrip("/"))
