import os
import time
import yaml
import requests
from datetime import datetime, timedelta, timezone
from atproto import Client, client_utils

ORCID_API_BASE = "https://pub.orcid.org/v3.0"
MAX_CHARS = 300


def load_config():
    print("Loading config.yaml")
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    print("Loaded config:", cfg)
    return cfg


def fetch_orcid_name(orcid_id: str) -> str:
    """
    Fetch the display name for an ORCID id from the public ORCID API.
    Falls back to the ORCID id string if anything goes wrong.
    """
    url = f"{ORCID_API_BASE}/{orcid_id}/person"
    headers = {"Accept": "application/vnd.orcid+json"}
    print(f"Requesting ORCID person record for {orcid_id}: {url}")

    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Failed to fetch name for {orcid_id}: {e}")
        return orcid_id

    name = data.get("name", {}) or {}
    given = (name.get("given-names", {}) or {}).get("value") or ""
    family = (name.get("family-name", {}) or {}).get("value") or ""

    full_name = " ".join(part for part in [given, family] if part).strip()
    if not full_name:
        full_name = orcid_id

    print(f"  Resolved {orcid_id} to name: {full_name}")
    return full_name


def fetch_works(orcid_id: str):
    url = f"{ORCID_API_BASE}/{orcid_id}/works"
    headers = {"Accept": "application/vnd.orcid+json"}
    print(f"Requesting ORCID works for {orcid_id}: {url}")
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        groups = data.get("group", []) or []
        print(f"  Retrieved {len(groups)} groups for {orcid_id}")
    except Exception as e:
        print(f"  Failed to fetch works for {orcid_id}: {e}")
        groups = []
    return groups


def filter_recent(groups, days: int):
    """
    Keep works whose CREATED date is within the last `days`.
    Fall back to last-modified-date only if created-date is missing.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    print(f"  Filtering works with created-date >= {cutoff.isoformat()}")
    results = []

    for g in groups:
        for ws in g.get("work-summary", []) or []:
            created_ts = ws.get("created-date", {}).get("value")
            modified_ts = ws.get("last-modified-date", {}).get("value")

            ts_to_use = created_ts or modified_ts
            if not ts_to_use:
                continue

            dt = datetime.fromtimestamp(int(ts_to_use) / 1000, tz=timezone.utc)
            if dt < cutoff:
                continue

            # Title
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

            results.append(
                {
                    "title": title,
                    "url": url,
                    "date": dt,
                }
            )

    print(f"  Found {len(results)} works after filtering by created-date")
    # newest first by created-date
    return sorted(results, key=lambda x: x["date"], reverse=True)


def build_post_builder(
    author_name: str,
    orcid_profile_url: str,
    title: str,
    doi_url: str | None,
    hashtags: list[str],
):
    """
    Build a Bluesky post with links and tags, truncating the title if needed
    to stay within MAX_CHARS characters.
    """

    def make_builder(current_title: str):
        b = client_utils.TextBuilder()

        # Name line, with ORCID link
        b.text("New paper from ")
        b.link(author_name, orcid_profile_url)

        # Title
        b.text("\n" + current_title)

        # DOI line
        if doi_url:
            b.text("\n")
            b.link(doi_url, doi_url)

        # Hashtags line
        if hashtags:
            b.text("\n")
            for i, tag in enumerate(hashtags):
                clean = tag.lstrip("#")
                visible = "#" + clean
                if i < len(hashtags) - 1:
                    visible += " "
                b.tag(visible, clean)

        return b

    # First try with full title
    builder = make_builder(title)
    text = builder.build_text()
    if len(text) <= MAX_CHARS:
        return builder

    # If too long, compute how much room we have for the title
    overhead = len(text) - len(title)  # everything except the title
    allowed = MAX_CHARS - overhead - 1  # minus 1 for ellipsis

    if allowed <= 0:
        allowed = 1

    short = title[:allowed]
    # Avoid cutting in the middle of a word if possible
    if " " in short:
        short = short.rsplit(" ", 1)[0]
    short = short + "…"

    builder = make_builder(short)
    text2 = builder.build_text()
    print(f"  Truncated title, final length {len(text2)} characters")
    return builder


def main():
    cfg = load_config()

    handle = os.getenv("BLUESKY_HANDLE")
    app_pw = os.getenv("BLUESKY_APP_PASSWORD")
    if not handle or not app_pw:
        raise RuntimeError("BLUESKY_HANDLE or BLUESKY_APP_PASSWORD not set")

    hashtags = cfg.get("hashtags", [])
    print("Using hashtags:", hashtags)

    print("Logging in to Bluesky as", handle)
    client = Client()
    client.login(handle, app_pw)

    max_posts = int(cfg.get("max_posts_total", 5))
    posted = 0

    # Cache ORCID → name within a single run
    name_cache: dict[str, str] = {}

    for oid in cfg["orcid_ids"]:
        if posted >= max_posts:
            break

        print(f"\n=== Checking {oid} ===")

        if oid not in name_cache:
            name_cache[oid] = fetch_orcid_name(oid)
        author_name = name_cache[oid]

        orcid_profile_url = f"https://orcid.org/{oid}"

        groups = fetch_works(oid)
        items = filter_recent(groups, int(cfg["days_back"]))

        if not items:
            print(f"  No recent works for {oid}")
            continue

        for item in items:
            if posted >= max_posts:
                break

            builder = build_post_builder(
                author_name=author_name,
                orcid_profile_url=orcid_profile_url,
                title=item["title"],
                doi_url=item["url"],
                hashtags=hashtags,
            )

            text_preview = builder.build_text()
            print(
                "  Posting (preview):",
                text_preview.replace("\n", " | "),
                f"({len(text_preview)} chars)",
            )

            client.send_post(builder)

            posted += 1
            time.sleep(1)

    print(f"\nFinished. Posted {posted} items.")


if __name__ == "__main__":
    main()
