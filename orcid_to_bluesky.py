import os
import time
import yaml
import requests
from datetime import datetime, timedelta, timezone
from atproto import Client

ORCID_API_BASE = "https://pub.orcid.org/v3.0"


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_works(orcid_id):
    url = f"{ORCID_API_BASE}/{orcid_id}/works"
    headers = {"Accept": "application/vnd.orcid+json"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json().get("group", [])


def filter_recent(groups, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    results = []

    for g in groups:
        for ws in g.get("work-summary", []):
            ts = ws.get("last-modified-date", {}).get("value")
            if not ts:
                continue

            dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            if dt < cutoff:
                continue

            title = ws.get("title", {}).get("title", {}).get("value", "(no title)")

            # Try DOI
            url = None
            for ext in ws.get("external-ids", {}).get("external-id", []):
                if ext.get("external-id-type", "").lower() == "doi":
                    url = "https://doi.org/" + ext.get("external-id-value")
                    break

            results.append({"title": title, "url": url, "date": dt})

    # newest first
    return sorted(results, key=lambda x: x["date"], reverse=True)


def main():
    cfg = load_config()

    client = Client()
    client.login(os.getenv("BLUESKY_HANDLE"), os.getenv("BLUESKY_APP_PASSWORD"))

    max_posts = cfg.get("max_posts_total", 5)
    posted = 0

    for oid in cfg["orcid_ids"]:
        if posted >= max_posts:
            break

        print(f"Checking {oid}")
        items = filter_recent(fetch_works(oid), cfg["days_back"])

        for item in items:
            if posted >= max_posts:
                break
            
            text = f"New publication from {oid}\n{item['title']}"
            if item["url"]:
                text += f"\n{item['url']}"

            print("Posting:", text.replace("\n", " | "))
            client.send_post(text)
            posted += 1
            time.sleep(1)

    print(f"Finished. Posted {posted} items.")
