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
    f"{platform['supabase_url']}/rest/v1/view_payments_with_profiles",
    headers={
        "apikey": platform["supabase_anon_key"],
        "Authorization": f"Bearer {token}",
    },
    params={"limit": 1, "order": "id.desc"},
    timeout=60,
)

print("HTTP", response.status_code)
print(json.dumps(response.json(), indent=2, ensure_ascii=False))
