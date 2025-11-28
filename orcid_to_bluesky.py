import os
import time
import yaml
import requests
from datetime import datetime, timedelta, timezone
from atproto import Client

ORCID_API_BASE = "https://pub.orcid.org/v3.0"


def load_config():
    print("Loading config.yaml")
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    print("Loaded config:", cfg)
    return cfg


def fetch_works(orcid_id):
    url = f"{ORCID_API_BASE}/{orcid_id}/works"
    headers = {"Accept": "application/vnd.orcid+json"}
    print(f"Requesting ORCID works for {orcid_id}: {url}")
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    groups = data.get("group", []) or []
    print(f"  Retrieved {len(groups)} groups for {orcid_id}")
    return groups


def filter_recent(groups, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    print(f"  Filtering works with last-modified-date >= {cutoff.isoformat()}")
    results = []

    for g in groups:
        for ws in g.get("work-summary", []) or []:
            ts = ws.get("last-modified-date", {}).get("value")
            if not ts:
                continue

            dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            if dt < cutoff:
                continue

            # Title handling a bit more defensively
            title_obj = ws.get("title", {}) or {}
            tval = title_obj.get("title", {}) or {}
            if isinstance(tval, dict):
                title = tval.get("value", "(no title)")
            elif isinstance(tval, str):
                title = tval
            else:
                title = "(no title)"

            # Try DOI
            url = None
            for ext in ws.get("external-ids", {}).get("external-id", []) or []:
                if (ext.get("external-id-type") or "").lower() == "doi":
                    val = ext.get("external-id-value")
                    if val:
                        url = "https://doi.org/" + val
                        break

            results.append({"title": title, "url": url, "date": dt})

    print(f"  Found {len(results)} works after filtering")
    # newest first
    return sorted(results, key=lambda x: x["date"], reverse=True)


def main():
    cfg = load_config()

    handle = os.getenv("BLUESKY_HANDLE")
    app_pw = os.getenv("BLUESKY_APP_PASSWORD")
    if not handle or not app_pw:
        raise RuntimeError("BLUESKY_HANDLE or BLUESKY_APP_PASSWORD not set")

    print("Logging in to Bluesky as", handle)
    client = Client()
    client.login(handle, app_pw)

    max_posts = cfg.get("max_posts_total", 5)
    posted = 0

    for oid in cfg["orcid_ids"]:
        if posted >= max_posts:
            break

        print(f"\n=== Checking {oid} ===")
        groups = fetch_works(oid)
        items = filter_recent(groups, cfg["days_back"])

        if not items:
            print(f"  No recent works for {oid}")
            continue

        for item in items:
            if posted >= max_posts:
                break

            text = f"New publication from {oid}\n{item['title']}"
            if item["url"]:
                text += f"\n{item['url']}"

            print("  Posting:", text.replace("\n", " | "))
            client.send_post(text)
            posted += 1
            time.sleep(1)

    print(f"\nFinished. Posted {posted} items.")


if __name__ == "__main__":
    main()
