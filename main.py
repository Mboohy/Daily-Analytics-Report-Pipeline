import json
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
import gspread
from gspread_dataframe import set_with_dataframe
from google.oauth2.service_account import Credentials

CONFIG_PATH = Path(__file__).parent / "config.json"
SCHEMA_PATH = Path(__file__).parent / "schema.json"


def load_json(path):
    if not path.exists():
        raise FileNotFoundError(f"Missing {path.name}.")

    with path.open() as f:
        return json.load(f)


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CONFIG_PATH.name}. "
            "Copy config.example.json to config.json and fill in your values."
        )

    return load_json(CONFIG_PATH)


def load_schema():
    return load_json(SCHEMA_PATH)


def filename_from_title(title):
    name = title.strip()

    for char in r'<>:"/\|?*':
        name = name.replace(char, "_")

    name = re.sub(r"\s+", "_", name).strip(" ._")

    return f"{name or 'platform'}.xlsx"


def platform_targets(config, require_credentials=True):
    platforms = config.get("platforms")

    if not platforms:
        raise ValueError("config.json must include a non-empty 'platforms' list.")

    seen = set()

    for index, platform in enumerate(platforms):
        title = (platform.get("title") or "").strip()

        if not title:
            raise ValueError(f"platforms[{index}] is missing 'title'.")

        if title in seen:
            raise ValueError(f"Duplicate platform title: {title}")

        seen.add(title)

        if require_credentials:
            if not platform.get("supabase_url") or not platform.get("supabase_anon_key"):
                raise ValueError(
                    f"Platform '{title}' needs supabase_url and supabase_anon_key."
                )

    return platforms


def with_platform(config, platform):
    merged = {
        key: value
        for key, value in config.items()
        if key != "platforms"
    }
    merged["title"] = platform["title"]
    merged["supabase_url"] = platform["supabase_url"]
    merged["supabase_anon_key"] = platform["supabase_anon_key"]
    return merged


def table_columns(schema, table):
    table_schema = schema.get(table)

    if not table_schema:
        known = ", ".join(sorted(schema)) or "(none)"
        raise ValueError(f"Unknown table '{table}'. Known tables: {known}")

    return table_schema["columns"]


def validate_columns(config, schema):
    columns = table_columns(schema, config["table"])
    unknown = [name for name in config["columns"] if name not in columns]

    if unknown:
        known = ", ".join(columns)
        raise ValueError(
            f"Unknown column(s) for '{config['table']}': {', '.join(unknown)}. "
            f"Known columns: {known}"
        )


def login(config):
    print(f"Logging in to {config['title']}...")

    response = requests.post(
        f"{config['supabase_url']}/auth/v1/token?grant_type=password",
        headers={
            "apikey": config["supabase_anon_key"],
            "Content-Type": "application/json",
        },
        json={
            "email": config["email"],
            "password": config["password"],
        },
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    print("Login successful.")

    return data["access_token"]


def get_page(config, access_token, page_size, after_id=None):
    select_columns = list(config["columns"])

    if "id" not in select_columns:
        select_columns = ["id", *select_columns]

    params = {
        "select": ",".join(select_columns),
        "order": "id.asc",
        "limit": page_size,
    }

    if after_id is not None:
        params["id"] = f"gt.{after_id}"

    response = requests.get(
        f"{config['supabase_url']}/rest/v1/{config['table']}",
        headers={
            "apikey": config["supabase_anon_key"],
            "Authorization": f"Bearer {access_token}",
        },
        params=params,
        timeout=60,
    )

    response.raise_for_status()

    return response.json()


def fetch_all(config, access_token):
    all_rows = []
    after_id = None
    page_size = config["page_size"]
    total_limit = config.get("limit")

    while True:
        remaining = None
        if total_limit is not None:
            remaining = total_limit - len(all_rows)
            if remaining <= 0:
                break

        this_page_size = page_size if remaining is None else min(page_size, remaining)

        if after_id is None:
            print(f"Fetching first {this_page_size} rows...")
        else:
            print(f"Fetching next {this_page_size} rows after {after_id}...")

        rows = get_page(config, access_token, this_page_size, after_id)

        if not rows:
            break

        all_rows.extend(rows)

        if len(rows) < this_page_size:
            break

        after_id = rows[-1]["id"]

    print(f"Received {len(all_rows)} total rows from Supabase.")
    return all_rows


def is_missing(value):
    if value is None:
        return True

    if isinstance(value, (list, dict)):
        return False

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def parse_json_list(value):
    if is_missing(value):
        return []

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        value = json.loads(value)

    if isinstance(value, dict):
        return [value]

    if isinstance(value, list):
        return value

    return []


def format_answer(answer):
    if is_missing(answer):
        return None

    if isinstance(answer, list):
        parts = [str(item) for item in answer if not is_missing(item)]
        return ", ".join(parts) if parts else None

    if isinstance(answer, dict):
        return json.dumps(answer, ensure_ascii=False)

    return str(answer)


def question_sort_key(question_id):
    try:
        return (0, int(question_id))
    except (TypeError, ValueError):
        return (1, str(question_id))


def expand_question_column(df, column_name, expand):
    question_field = expand["question_field"]
    answer_field = expand["answer_field"]
    id_field = expand["id_field"]

    parsed_items = [parse_json_list(value) for value in df[column_name]]

    labels_by_id = {}
    for items in parsed_items:
        for item in items:
            if not isinstance(item, dict):
                continue

            question_id = item.get(id_field)
            if question_id is None or question_id in labels_by_id:
                continue

            label = item.get(question_field)
            if not label:
                label = f"question_{question_id}"

            labels_by_id[question_id] = str(label)

    label_counts = {}
    for label in labels_by_id.values():
        label_counts[label] = label_counts.get(label, 0) + 1

    unique_labels = {
        question_id: f"{label} [{question_id}]" if label_counts[label] > 1 else label
        for question_id, label in labels_by_id.items()
    }

    parsed_rows = []
    unlabeled_order = []

    for items in parsed_items:
        row_answers = {}

        for item in items:
            if not isinstance(item, dict):
                continue

            question_id = item.get(id_field)
            answer = format_answer(item.get(answer_field))

            if question_id in unique_labels:
                row_answers[unique_labels[question_id]] = answer
                continue

            label = item.get(question_field)
            if not label:
                continue

            label = str(label)
            row_answers[label] = answer

            if label not in unlabeled_order and label not in unique_labels.values():
                unlabeled_order.append(label)

        parsed_rows.append(row_answers)

    ordered_labels = [
        unique_labels[question_id]
        for question_id in sorted(unique_labels, key=question_sort_key)
    ]
    ordered_labels.extend(unlabeled_order)

    expanded = pd.DataFrame(parsed_rows, index=df.index).reindex(columns=ordered_labels)

    location = df.columns.get_loc(column_name)
    before = df.iloc[:, :location]
    after = df.iloc[:, location + 1 :]

    return pd.concat([before, expanded, after], axis=1)


def expand_json_columns(df, config, schema):
    columns = table_columns(schema, config["table"])

    for name in list(df.columns):
        meta = columns.get(name)

        if meta and meta.get("expand"):
            df = expand_question_column(df, name, meta["expand"])

    return df


def apply_column_types(df, config, schema):
    columns = table_columns(schema, config["table"])

    for name in df.columns:
        meta = columns.get(name)

        if not meta:
            continue

        if meta.get("expand"):
            continue

        column_type = meta["type"]

        if column_type in ("uuid", "text"):
            df[name] = df[name].astype("string")
        elif column_type == "boolean":
            df[name] = df[name].astype("boolean")
        elif column_type == "timestamptz":
            df[name] = pd.to_datetime(df[name], utc=True, format="ISO8601").dt.tz_localize(None)
        elif column_type == "date":
            df[name] = pd.to_datetime(df[name], utc=False, format="ISO8601").dt.date
        elif column_type == "jsonb":
            df[name] = df[name].map(
                lambda value: json.dumps(value)
                if value is not None and not isinstance(value, str)
                else value
            )
        elif column_type == "text[]":
            df[name] = df[name].map(format_answer)

    return df


def export_excel(config, schema, rows, output_file):
    print(f"Processing and exporting {len(rows)} rows...")

    df = pd.DataFrame(rows, columns=config["columns"])
    df = expand_json_columns(df, config, schema)
    df = apply_column_types(df, config, schema)

    df.to_excel(
        output_file,
        index=False,
        engine="openpyxl",
    )

    return df


def export_platform(config, schema, platform):
    target = with_platform(config, platform)
    output_file = filename_from_title(platform["title"])

    print(f"\n=== {platform['title']} ===")

    access_token = login(target)
    rows = fetch_all(target, access_token)

    if not rows:
        print(f"⚠️ No data found for {platform['title']}.")
        df = pd.DataFrame(columns=config["columns"])
        df.to_excel(output_file, index=False, engine="openpyxl")
        return df

    return export_excel(target, schema, rows, output_file)


def load_platform_excel(title):
    path = Path(filename_from_title(title))

    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Set fetch to true to download it, "
            "or place the Excel file next to the script."
        )

    print(f"Reading {path}...")
    return pd.read_excel(path, engine="openpyxl")


def email_key(value):
    if is_missing(value):
        return None

    key = str(value).strip().lower()
    return key or None


def stringify_value(value):
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(value, date):
        return value.isoformat()

    return str(value)


def values_from_cell(value):
    if is_missing(value):
        return []

    if isinstance(value, list):
        parts = []
        for item in value:
            parts.extend(values_from_cell(item))
        return parts

    if isinstance(value, (pd.Timestamp, datetime, date)):
        return [stringify_value(value)]

    text = str(value).strip()

    if not text:
        return []

    if ", " in text:
        return [part.strip() for part in text.split(", ") if part.strip()]

    return [text]


def unique_extend(values, incoming):
    for item in incoming:
        if item not in values:
            values.append(item)


def format_merged(values):
    if not values:
        return None

    if len(values) == 1:
        return values[0]

    return ", ".join(stringify_value(item) for item in values)


def consolidated_columns(frames):
    ordered = []
    seen = set()

    for df in frames.values():
        for name in df.columns:
            if name not in seen:
                seen.add(name)
                ordered.append(name)

    if "email" in ordered:
        index = ordered.index("email") + 1
        ordered.insert(index, "platforms")
    else:
        ordered.insert(0, "platforms")

    return ordered


def consolidate_frames(frames, output_file="consolidated.xlsx"):
    if not frames:
        raise ValueError("No platform spreadsheets to consolidate.")

    valid_frames = {title: df for title, df in frames.items() if not df.empty}
    
    if not valid_frames:
        print("⚠️ Warning: All platform DataFrames are empty. Consolidated sheet will be empty.")
        columns = consolidated_columns(frames)
        return pd.DataFrame(columns=columns)

    has_email = any("email" in df.columns for df in valid_frames.values())

    # Fallback to simple concatenation if no email column is present
    if not has_email:
        print("⚠️ Warning: No 'email' column found. Combining all frames directly.")
        combined_list = []
        for title, df in valid_frames.items():
            temp_df = df.copy()
            temp_df["platforms"] = title
            combined_list.append(temp_df)
        result = pd.concat(combined_list, ignore_index=True)
        result.to_excel(output_file, index=False, engine="openpyxl")
        return result

    groups = {}
    group_order = []

    for title, df in valid_frames.items():
        for index, row in df.iterrows():
            key = email_key(row["email"]) if "email" in df.columns else None

            if key is None:
                group_key = ("missing", title, index)
            else:
                group_key = ("email", key)

            if group_key not in groups:
                groups[group_key] = {
                    "platforms": [],
                    "values": {},
                    "email": None,
                }
                group_order.append(group_key)

            group = groups[group_key]
            unique_extend(group["platforms"], [title])

            if group["email"] is None and "email" in df.columns and not is_missing(row["email"]):
                group["email"] = str(row["email"]).strip()

            for column in df.columns:
                if column == "email":
                    continue

                group["values"].setdefault(column, [])
                unique_extend(group["values"][column], values_from_cell(row[column]))

    columns = consolidated_columns(valid_frames)
    merged_rows = []

    for group_key in group_order:
        group = groups[group_key]
        merged = {}

        for column in columns:
            if column == "platforms":
                merged[column] = format_merged(group["platforms"])
            elif column == "email":
                merged[column] = group["email"]
            else:
                merged[column] = format_merged(group["values"].get(column, []))

        merged_rows.append(merged)

    result = pd.DataFrame(merged_rows, columns=columns)

    print(f"Exporting {len(result)} consolidated rows to {output_file}...")
    result.to_excel(output_file, index=False, engine="openpyxl")
    print(f"Done: {output_file}")

    return result


def update_google_sheet(df, sheet_id, tab_name):
    print(f"Updating Google Sheet tab '{tab_name}' with {len(df)} rows...")
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    
    credentials_path = Path(__file__).parent / "credentials.json"
    if not credentials_path.exists():
        print("Warning: credentials.json not found. Skipping Google Sheets update.")
        return

    try:
        credentials = Credentials.from_service_account_file(credentials_path, scopes=scopes)
        gc = gspread.authorize(credentials)
        sheet = gc.open_by_key(sheet_id)
        
        try:
            worksheet = sheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sheet.add_worksheet(title=tab_name, rows="1000", cols="20")
            
        worksheet.clear()
        
        safe_df = df.copy().astype(str)
        safe_df = safe_df.replace(["nan", "NaT", "<NA>", "None"], "")
        
        set_with_dataframe(worksheet, safe_df)
        print(f"Tab '{tab_name}' updated successfully.")
    except Exception as e:
        print(f"Failed to update Google Sheet tab '{tab_name}': {e}")


def print_summary(frames, consolidated=None):
    print("\n=== Summary ===")

    total = 0

    for title, df in frames.items():
        count = len(df)
        total += count
        print(f"  {title}: {count} students")

    print(f"  Total students: {total}")

    emails = set()
    missing = 0

    for title, df in frames.items():
        if "email" not in df.columns:
            missing += len(df)
            continue

        for value in df["email"]:
            key = email_key(value)

            if key is None:
                missing += 1
            else:
                emails.add(key)

    distinct = len(emails) + missing
    print(f"  Distinct students: {distinct}")

    if missing:
        print(f"  ({len(emails)} with email, {missing} without)")


def main():
    try:
        config = load_config()
        fetch = bool(config.get("fetch", True))
        consolidate = bool(config.get("consolidate", False))

        if not fetch and not consolidate:
            raise ValueError("Enable fetch and/or consolidate in config.json.")

        platforms = platform_targets(config, require_credentials=fetch)
        frames = {}
        failures = []

        if fetch:
            schema = load_schema()
            validate_columns(config, schema)

            for platform in platforms:
                title = platform["title"]

                try:
                    frames[title] = export_platform(config, schema, platform)
                except requests.HTTPError as e:
                    print(f"Supabase request failed for {title}:")

                    if e.response is not None:
                        print(f"HTTP {e.response.status_code}")
                        print(e.response.text)

                    failures.append(title)
                except Exception as e:
                    print(f"Error for {title}: {e}")
                    failures.append(title)
        else:
            for platform in platforms:
                title = platform["title"]

                try:
                    frames[title] = load_platform_excel(title)
                except Exception as e:
                    print(f"Error for {title}: {e}")
                    failures.append(title)

        consolidated = None
        sheet_id = "1j5wS-qr6No0uWSr4p_7jbYYCsqVqr17s7Bx1wTlpuwc"

        if frames:
            for title, df in frames.items():
                update_google_sheet(df, sheet_id, title)

        if consolidate and frames:
            print("\n=== Consolidate ===")
            consolidated = consolidate_frames(frames)
            update_google_sheet(consolidated, sheet_id, "Consolidated")

        if frames:
            print_summary(frames, consolidated)

        if failures:
            raise RuntimeError("Failed platforms: " + ", ".join(failures))

    except requests.HTTPError as e:
        print("Supabase request failed:")

        if e.response is not None:
            print(f"HTTP {e.response.status_code}")
            print(e.response.text)

        raise

    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
