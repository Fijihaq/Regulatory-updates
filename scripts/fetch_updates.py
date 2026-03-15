#!/usr/bin/env python3
"""
Fetches regulatory updates from:
  - RSS feeds  : FCA (UK), SEC (US), ESMA (EU)
  - Web scraping: FATF (global), OFAC (US sanctions)
  - GNews API  : Reuters & Bloomberg regulatory news

Saves results to data/regulatory-updates.json

SETUP REQUIRED:
  Add GNEWS_API_KEY as a GitHub Actions secret (free at https://gnews.io)
  If GNEWS_API_KEY is not set, Reuters/Bloomberg news will be skipped gracefully.
"""

import feedparser
import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# ── Config ────────────────────────────────────────────────────────────────────
MAX_ITEMS_PER_SOURCE = 10
MAX_TOTAL_ITEMS      = 60
GNEWS_API_KEY        = os.environ.get("GNEWS_API_KEY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RegulatoryFeedBot/1.0; "
        "+https://github.com/your-username/your-repo)"
    )
}

# ── RSS feed sources ──────────────────────────────────────────────────────────
RSS_FEEDS = [
    {
        "source": "FCA (UK)",
        "url":    "https://www.fca.org.uk/news/rss.xml",
        "country": "UK",
        "flag":   "🇬🇧",
        "category": "Regulator",
    },
    {
        "source": "SEC (US)",
        "url":    "https://www.sec.gov/rss/news/press.rss",
        "country": "US",
        "flag":   "🇺🇸",
        "category": "Regulator",
    },
    {
        "source": "ESMA (EU)",
        "url":    "https://www.esma.europa.eu/rss.xml",
        "country": "EU",
        "flag":   "🇪🇺",
        "category": "Regulator",
    },
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_rss_date(entry) -> str:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                return parsedate_to_datetime(raw).isoformat()
            except Exception:
                pass
    return now_iso()


def make_item(title, link, summary, date, source, country, flag, category="Regulator") -> dict:
    return {
        "title":    title.strip(),
        "link":     link.strip(),
        "summary":  strip_html(summary)[:300],
        "date":     date,
        "source":   source,
        "country":  country,
        "flag":     flag,
        "category": category,
    }

# ── 1. RSS Fetcher ────────────────────────────────────────────────────────────

def fetch_rss(feed_cfg: dict) -> list[dict]:
    items = []
    try:
        parsed = feedparser.parse(feed_cfg["url"])
        for entry in parsed.entries[:MAX_ITEMS_PER_SOURCE]:
            title   = entry.get("title", "").strip()
            link    = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", ""))
            if not title:
                continue
            items.append(make_item(
                title, link, summary, safe_rss_date(entry),
                feed_cfg["source"], feed_cfg["country"],
                feed_cfg["flag"], feed_cfg.get("category", "Regulator")
            ))
    except Exception as exc:
        print(f"  ⚠  RSS failed for {feed_cfg['source']}: {exc}")
    return items

# ── 2. FATF Scraper ───────────────────────────────────────────────────────────

def fetch_fatf() -> list[dict]:
    """Scrape FATF news page (no public RSS available)."""
    items = []
    url = "https://www.fatf-gafi.org/en/the-fatf/news.html"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # FATF uses article cards with class patterns — try multiple selectors
        cards = (
            soup.select("div.media-story-card")
            or soup.select("article")
            or soup.select("div.list-item")
            or soup.select("li.publication-item")
        )

        for card in cards[:MAX_ITEMS_PER_SOURCE]:
            # Title and link
            a_tag = card.find("a", href=True)
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href  = a_tag["href"]
            link  = href if href.startswith("http") else f"https://www.fatf-gafi.org{href}"

            # Summary
            summary_tag = card.find(["p", "div"], class_=re.compile(r"desc|summary|excerpt|intro", re.I))
            summary = summary_tag.get_text(strip=True) if summary_tag else ""

            # Date
            date_tag = card.find(["time", "span", "div"], class_=re.compile(r"date|time|publish", re.I))
            date_str = now_iso()
            if date_tag:
                dt_attr = date_tag.get("datetime", "")
                if dt_attr:
                    try:
                        date_str = datetime.fromisoformat(dt_attr.replace("Z", "+00:00")).isoformat()
                    except Exception:
                        pass

            if not title:
                continue
            items.append(make_item(title, link, summary, date_str,
                                   "FATF", "GLOBAL", "🌍", "Regulator"))

        # Fallback: grab plain links if no cards found
        if not items:
            for a in soup.select("main a[href]")[:MAX_ITEMS_PER_SOURCE]:
                title = a.get_text(strip=True)
                href  = a["href"]
                if len(title) < 15:
                    continue
                link = href if href.startswith("http") else f"https://www.fatf-gafi.org{href}"
                items.append(make_item(title, link, "", now_iso(),
                                       "FATF", "GLOBAL", "🌍", "Regulator"))

    except Exception as exc:
        print(f"  ⚠  FATF scrape failed: {exc}")
    return items

# ── 3. OFAC Scraper ───────────────────────────────────────────────────────────

def fetch_ofac() -> list[dict]:
    """
    Scrape OFAC recent actions page.
    Note: OFAC retired its RSS feed on 31 Jan 2025.
    """
    items = []
    url = "https://ofac.treasury.gov/recent-actions"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # OFAC lists actions in a table or list structure
        rows = (
            soup.select("table tbody tr")
            or soup.select("div.views-row")
            or soup.select("li.views-row")
            or soup.select("article")
        )

        for row in rows[:MAX_ITEMS_PER_SOURCE]:
            a_tag = row.find("a", href=True)
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href  = a_tag["href"]
            link  = href if href.startswith("http") else f"https://ofac.treasury.gov{href}"

            # Date — OFAC often shows dates in td/span elements
            date_tag  = row.find(["td", "span", "div"], class_=re.compile(r"date|time|views-field-field-date", re.I))
            date_str  = now_iso()
            if date_tag:
                raw_date = date_tag.get_text(strip=True)
                # Try common US date format: "03/14/2025"
                try:
                    date_str = datetime.strptime(raw_date, "%m/%d/%Y").replace(
                        tzinfo=timezone.utc).isoformat()
                except Exception:
                    pass

            summary_tag = row.find(["td", "p", "div"], class_=re.compile(r"body|desc|summary", re.I))
            summary = summary_tag.get_text(strip=True) if summary_tag else ""

            if not title or len(title) < 5:
                continue
            items.append(make_item(title, link, summary, date_str,
                                   "OFAC (US)", "US", "🇺🇸", "Sanctions"))

    except Exception as exc:
        print(f"  ⚠  OFAC scrape failed: {exc}")
    return items

# ── 4. GNews API (Reuters + Bloomberg) ───────────────────────────────────────

def fetch_gnews(query: str, source_label: str, flag: str, country: str, category: str) -> list[dict]:
    """
    Fetch news via GNews free API (https://gnews.io).
    Free tier: 100 requests/day, 10 articles/request.
    Sign up at https://gnews.io to get a free API key.
    Store it as GNEWS_API_KEY in GitHub Actions secrets.
    """
    if not GNEWS_API_KEY:
        print(f"  ⚠  GNEWS_API_KEY not set — skipping {source_label}")
        return []

    items = []
    try:
        params = {
            "q":        query,
            "lang":     "en",
            "max":      MAX_ITEMS_PER_SOURCE,
            "apikey":   GNEWS_API_KEY,
            "sortby":   "publishedAt",
        }
        resp = requests.get("https://gnews.io/api/v4/search", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        for art in data.get("articles", []):
            title    = art.get("title", "").strip()
            link     = art.get("url", "").strip()
            summary  = art.get("description", "").strip()
            pub_date = art.get("publishedAt", "")

            # Normalise date
            date_str = now_iso()
            if pub_date:
                try:
                    date_str = datetime.fromisoformat(
                        pub_date.replace("Z", "+00:00")).isoformat()
                except Exception:
                    pass

            if not title:
                continue
            items.append(make_item(title, link, summary, date_str,
                                   source_label, country, flag, category))

        time.sleep(1)  # Rate limit courtesy pause
    except Exception as exc:
        print(f"  ⚠  GNews failed for {source_label}: {exc}")
    return items

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"🔍  Fetching regulatory feeds — {datetime.now(timezone.utc).isoformat()}\n")
    all_items: list[dict] = []

    # 1. RSS feeds
    for feed_cfg in RSS_FEEDS:
        print(f"  → [RSS] {feed_cfg['source']}")
        items = fetch_rss(feed_cfg)
        print(f"        {len(items)} items")
        all_items.extend(items)

    # 2. FATF (web scrape)
    print("  → [SCRAPE] FATF")
    items = fetch_fatf()
    print(f"        {len(items)} items")
    all_items.extend(items)

    # 3. OFAC (web scrape — RSS retired Jan 2025)
    print("  → [SCRAPE] OFAC")
    items = fetch_ofac()
    print(f"        {len(items)} items")
    all_items.extend(items)

    # 4. Reuters — financial regulation news via GNews
    print("  → [GNEWS] Reuters")
    items = fetch_gnews(
        query='site:reuters.com financial regulation OR sanctions OR compliance',
        source_label="Reuters",
        flag="📰",
        country="MEDIA",
        category="News"
    )
    print(f"        {len(items)} items")
    all_items.extend(items)

    # 5. Bloomberg — financial regulation news via GNews
    print("  → [GNEWS] Bloomberg")
    items = fetch_gnews(
        query='site:bloomberg.com financial regulation OR sanctions OR compliance',
        source_label="Bloomberg",
        flag="📊",
        country="MEDIA",
        category="News"
    )
    print(f"        {len(items)} items")
    all_items.extend(items)

    # Sort newest first, deduplicate by title
    seen_titles = set()
    deduped = []
    all_items.sort(key=lambda x: x["date"], reverse=True)
    for item in all_items:
        key = item["title"].lower()[:80]
        if key not in seen_titles:
            seen_titles.add(key)
            deduped.append(item)

    deduped = deduped[:MAX_TOTAL_ITEMS]

    # Build payload
    payload = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total":        len(deduped),
        "items":        deduped,
    }

    os.makedirs("data", exist_ok=True)
    out_path = "data/regulatory-updates.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\n✅  Saved {len(deduped)} items → {out_path}")


if __name__ == "__main__":
    main()
