import json
from pathlib import Path

import requests

config = json.loads(Path("config.json").read_text())

# Columns we already fetch successfully from every platform today.
BASELINE = {
    "id", "first_name", "last_name", "email", "phone_number", "date_of_birth",
    "sex", "country", "country_of_residence", "province", "city",
    "telegram_id", "is_confirmed", "is_verified", "last_signed_in",
    "last_active", "active_months", "enrollment_form_data", "created_at",
    "updated_at", "last_seen_announcements", "cohort_titles",
    "learning_path_titles", "course_titles",
}

results = {}

for platform in config["platforms"]:
    title = platform["title"]
    print(f"\n=== {title} ===")

    try:
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
            f"{platform['supabase_url']}/rest/v1/analytics_profiles",
            headers={
                "apikey": platform["supabase_anon_key"],
                "Authorization": f"Bearer {token}",
            },
            params={"select": "*", "limit": 1},
            timeout=60,
        )
        response.raise_for_status()
        rows = response.json()

        if isinstance(rows, list) and rows:
            cols = set(rows[0].keys())
        else:
            cols = set()
            print("  (0 rows returned, can't see columns this way)")

        results[title] = cols
        extra = sorted(cols - BASELINE)

        if extra:
            print(f"  أعمدة إضافية غير الأساسية: {extra}")
        else:
            print("  مفيش أعمدة إضافية غير الأساسية")

    except Exception as e:
        print(f"  Error: {e}")
        results[title] = None

# Summary: for each extra column, which platforms actually have it
print("\n\n=== ملخص: كل عمود إضافي وفين موجود ===")
all_extra = set()
for cols in results.values():
    if cols:
        all_extra |= (cols - BASELINE)

for col in sorted(all_extra):
    have = [t for t, cols in results.items() if cols and col in cols]
    missing = [t for t, cols in results.items() if cols is not None and col not in cols]
    print(f"{col}: موجود في {have} | غير موجود في {missing}")
