#!/usr/bin/env python3
"""
Fetches regulatory updates from:
  RSS feeds   : FCA, HMRC, NCA, SFO, ICO, EBA, ESMA, SEC, BIS, MAS
  Web scraping: FATF (no RSS), OFAC (RSS retired Jan 2025)
  GNews API   : AML / financial crime / sanctions news (optional)

Saves to: data/regulatory-updates.json

SETUP:
  Optional: add GNEWS_API_KEY as a GitHub Actions secret (free at gnews.io)
  All regulator sources work without an API key.
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

# ── Config ─────────────────────────────────────────────────────────────────
MAX_PER_SOURCE = 8
MAX_TOTAL      = 80
GNEWS_KEY      = os.environ.get("GNEWS_API_KEY", "")
TIMEOUT        = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# ── RSS feed sources ────────────────────────────────────────────────────────
RSS_FEEDS = [
    # ── UK regulators ──
    {
        "source": "FCA",
        "url":    "https://www.fca.org.uk/news/rss.xml",
        "country": "UK", "flag": "🇬🇧", "category": "Regulator",
        "tags": ["AML", "Financial Crime", "Conduct"],
    },
    {
        "source": "HMRC",
        "url":    "https://www.gov.uk/government/organisations/hm-revenue-customs.atom",
        "country": "UK", "flag": "🇬🇧", "category": "Regulator",
        "tags": ["AML", "Tax", "Financial Crime"],
    },
    {
        "source": "ICO",
        "url":    "https://ico.org.uk/about-the-ico/media-centre/rss/",
        "country": "UK", "flag": "🇬🇧", "category": "Regulator",
        "tags": ["Data", "Privacy"],
    },
    {
        "source": "NCA",
        "url":    "https://www.nationalcrimeagency.gov.uk/rss.xml",
        "country": "UK", "flag": "🇬🇧", "category": "Regulator",
        "tags": ["AML", "SAR", "Financial Crime"],
    },
    {
        "source": "SFO",
        "url":    "https://www.sfo.gov.uk/feed/",
        "country": "UK", "flag": "🇬🇧", "category": "Regulator",
        "tags": ["AML", "Fraud", "Enforcement"],
    },
    # ── EU regulators ──
    {
        "source": "ESMA",
        "url":    "https://www.esma.europa.eu/rss.xml",
        "country": "EU", "flag": "🇪🇺", "category": "Regulator",
        "tags": ["Market Integrity", "Capital Markets"],
    },
    {
        "source": "EBA",
        "url":    "https://www.eba.europa.eu/rss.xml",
        "country": "EU", "flag": "🇪🇺", "category": "Regulator",
        "tags": ["AML", "Prudential", "Banking"],
    },
    # ── US regulators ──
    {
        "source": "SEC",
        "url":    "https://www.sec.gov/rss/news/press.rss",
        "country": "US", "flag": "🇺🇸", "category": "Regulator",
        "tags": ["Securities", "Enforcement"],
    },
    {
        "source": "FinCEN",
        "url":    "https://www.fincen.gov/news-room/rss.xml",
        "country": "US", "flag": "🇺🇸", "category": "Regulator",
        "tags": ["AML", "Financial Crime", "Sanctions"],
    },
    # ── Global / other ──
    {
        "source": "BIS",
        "url":    "https://www.bis.org/rss/index.htm",
        "country": "GLOBAL", "flag": "🌍", "category": "Regulator",
        "tags": ["Prudential", "Basel"],
    },
    {
        "source": "MAS",
        "url":    "https://www.mas.gov.sg/rss/news",
        "country": "SG", "flag": "🇸🇬", "category": "Regulator",
        "tags": ["AML", "Crypto", "Fintech"],
    },
    {
        "source": "AUSTRAC",
        "url":    "https://www.austrac.gov.au/news-and-media/rss.xml",
        "country": "AU", "flag": "🇦🇺", "category": "Regulator",
        "tags": ["AML", "Financial Crime"],
    },
]

# AML keyword list for tagging items
AML_KEYWORDS = [
    "anti-money laundering", "aml", "money laundering", "financial crime",
    "suspicious activity", "sar", "suspicious transaction", "str",
    "know your customer", "kyc", "customer due diligence", "cdd",
    "beneficial ownership", "beneficial owner", "pep", "politically exposed",
    "money mule", "proceeds of crime", "terrorist financing", "ctf",
    "financial intelligence", "proceeds", "layering", "placement",
    "integration", "unexplained wealth", "unexplained", "illicit finance",
]

# ── Helpers ─────────────────────────────────────────────────────────────────

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


def is_aml_related(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in AML_KEYWORDS)


def make_item(title, link, summary, date, source, country, flag,
              category="Regulator", tags=None, is_aml=False) -> dict:
    clean_summary = strip_html(summary)[:400]
    return {
        "title":    title.strip(),
        "link":     link.strip(),
        "summary":  clean_summary,
        "date":     date,
        "source":   source,
        "country":  country,
        "flag":     flag,
        "category": category,
        "tags":     tags or [],
        "is_aml":   is_aml or is_aml_related(title + " " + clean_summary),
        "is_uk":    country == "UK",
    }

# ── 1. RSS Fetcher ───────────────────────────────────────────────────────────

def fetch_rss(feed_cfg: dict) -> list[dict]:
    items = []
    try:
        parsed = feedparser.parse(
            feed_cfg["url"],
            request_headers={"User-Agent": HEADERS["User-Agent"]}
        )
        if parsed.bozo and not parsed.entries:
            print(f"        ⚠  bozo parse: {parsed.bozo_exception}")
            return items

        for entry in parsed.entries[:MAX_PER_SOURCE]:
            title   = entry.get("title", "").strip()
            link    = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", ""))
            if not title or len(title) < 10:
                continue
            items.append(make_item(
                title, link, summary, safe_rss_date(entry),
                feed_cfg["source"], feed_cfg["country"],
                feed_cfg["flag"], feed_cfg.get("category", "Regulator"),
                feed_cfg.get("tags", [])
            ))
    except Exception as exc:
        print(f"        ⚠  RSS error for {feed_cfg['source']}: {exc}")
    return items

# ── 2. FATF Scraper ──────────────────────────────────────────────────────────

def fetch_fatf() -> list[dict]:
    """
    FATF changed their site in 2024. This scraper targets
    their publications listing page with multiple fallback selectors.
    """
    items = []
    urls_to_try = [
        "https://www.fatf-gafi.org/en/the-fatf/news.html",
        "https://www.fatf-gafi.org/en/publications.html",
    ]

    for url in urls_to_try:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Try multiple card selectors (FATF site varies)
            card_selectors = [
                "div.search-result-item",
                "div.publication-item",
                "article.card",
                "div.content-listing__item",
                "li.publication",
                "div[class*='card']",
                "div[class*='item']",
            ]

            cards = []
            for sel in card_selectors:
                cards = soup.select(sel)
                if len(cards) >= 3:
                    break

            # Absolute fallback: all meaningful anchor tags in main content
            if len(cards) < 3:
                main = soup.find("main") or soup.find("div", id="main") or soup
                anchors = [
                    a for a in main.find_all("a", href=True)
                    if len(a.get_text(strip=True)) > 20
                ]
                for a in anchors[:MAX_PER_SOURCE]:
                    title = a.get_text(strip=True)
                    href  = a["href"]
                    link  = href if href.startswith("http") else f"https://www.fatf-gafi.org{href}"
                    items.append(make_item(
                        title, link, "", now_iso(),
                        "FATF", "GLOBAL", "🌍", "Regulator", ["AML", "Financial Crime"]
                    ))
                if items:
                    break
                continue

            for card in cards[:MAX_PER_SOURCE]:
                a_tag = card.find("a", href=True)
                if not a_tag:
                    continue

                title = a_tag.get_text(strip=True)
                if len(title) < 15:
                    # Try broader text within the card
                    title = card.get_text(separator=" ", strip=True)[:120]

                href = a_tag["href"]
                link = href if href.startswith("http") else f"https://www.fatf-gafi.org{href}"

                # Summary
                p_tag = card.find("p")
                summary = p_tag.get_text(strip=True) if p_tag else ""

                # Date — look for <time> or common date patterns
                date_str = now_iso()
                time_tag = card.find("time")
                if time_tag:
                    dt_attr = time_tag.get("datetime", time_tag.get_text(strip=True))
                    try:
                        date_str = datetime.fromisoformat(
                            dt_attr.replace("Z", "+00:00")).isoformat()
                    except Exception:
                        # Try parsing plain text date like "14 March 2025"
                        try:
                            date_str = datetime.strptime(dt_attr, "%d %B %Y").replace(
                                tzinfo=timezone.utc).isoformat()
                        except Exception:
                            pass
                else:
                    # Search card text for date pattern: "14 March 2025" or "March 14, 2025"
                    card_text = card.get_text()
                    date_match = re.search(
                        r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
                        r"September|October|November|December)\s+(\d{4})\b",
                        card_text
                    )
                    if date_match:
                        try:
                            date_str = datetime.strptime(
                                date_match.group(), "%d %B %Y").replace(
                                tzinfo=timezone.utc).isoformat()
                        except Exception:
                            pass

                if not title:
                    continue

                items.append(make_item(
                    title, link, summary, date_str,
                    "FATF", "GLOBAL", "🌍", "Regulator", ["AML", "Financial Crime"]
                ))

            if items:
                break  # Got results from this URL, stop trying more

        except requests.HTTPError as e:
            print(f"        ⚠  FATF HTTP {e.response.status_code} from {url}")
        except Exception as exc:
            print(f"        ⚠  FATF scrape error ({url}): {exc}")

    return items

# ── 3. OFAC Scraper ──────────────────────────────────────────────────────────

def fetch_ofac() -> list[dict]:
    """
    OFAC retired their RSS feed on 31 Jan 2025.
    Scrapes the recent-actions page — structure as of 2025.
    """
    items = []
    url = "https://ofac.treasury.gov/recent-actions"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # OFAC recent-actions: rows inside a views table or div list
        row_selectors = [
            "table tbody tr",
            "div.view-content div.views-row",
            "ul.view-content li",
            "div.item-list li",
            "article",
        ]

        rows = []
        for sel in row_selectors:
            rows = soup.select(sel)
            if len(rows) >= 2:
                break

        # Absolute fallback: anchor links in main content
        if len(rows) < 2:
            main = soup.find("main") or soup.find("div", class_=re.compile(r"content|main", re.I)) or soup
            for a in main.find_all("a", href=True)[:MAX_PER_SOURCE]:
                title = a.get_text(strip=True)
                if len(title) < 10:
                    continue
                href = a["href"]
                link = href if href.startswith("http") else f"https://ofac.treasury.gov{href}"
                items.append(make_item(
                    title, link, "", now_iso(),
                    "OFAC", "US", "🇺🇸", "Sanctions", ["Sanctions"]
                ))
            return items

        for row in rows[:MAX_PER_SOURCE]:
            a_tag = row.find("a", href=True)
            if not a_tag:
                continue

            title = a_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            href = a_tag["href"]
            link = href if href.startswith("http") else f"https://ofac.treasury.gov{href}"

            # Date extraction — OFAC format: "03/14/2025" or "March 14, 2025"
            date_str = now_iso()
            row_text = row.get_text(separator=" ")

            # US date pattern MM/DD/YYYY
            us_date = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", row_text)
            if us_date:
                try:
                    date_str = datetime.strptime(us_date.group(), "%m/%d/%Y").replace(
                        tzinfo=timezone.utc).isoformat()
                except Exception:
                    pass
            else:
                # Long month pattern
                long_date = re.search(
                    r"\b(January|February|March|April|May|June|July|August|"
                    r"September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
                    row_text
                )
                if long_date:
                    try:
                        date_str = datetime.strptime(
                            long_date.group(), "%B %d, %Y").replace(
                            tzinfo=timezone.utc).isoformat()
                    except Exception:
                        pass

            # Summary — second td or any p
            summary = ""
            tds = row.find_all("td")
            if len(tds) >= 2:
                summary = tds[-1].get_text(strip=True)

            items.append(make_item(
                title, link, summary, date_str,
                "OFAC", "US", "🇺🇸", "Sanctions", ["Sanctions"]
            ))

    except requests.HTTPError as e:
        print(f"        ⚠  OFAC HTTP {e.response.status_code}")
    except Exception as exc:
        print(f"        ⚠  OFAC scrape error: {exc}")

    return items

# ── 4. GNews (optional) ──────────────────────────────────────────────────────

def fetch_gnews(query: str, source_label: str, flag: str,
                country: str, category: str, tags: list) -> list[dict]:
    if not GNEWS_KEY:
        return []
    items = []
    try:
        params = {
            "q":      query,
            "lang":   "en",
            "max":    MAX_PER_SOURCE,
            "apikey": GNEWS_KEY,
            "sortby": "publishedAt",
        }
        resp = requests.get("https://gnews.io/api/v4/search",
                            params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        for art in data.get("articles", []):
            title   = (art.get("title") or "").strip()
            link    = (art.get("url") or "").strip()
            summary = (art.get("description") or "").strip()
            pub     = art.get("publishedAt", "")

            date_str = now_iso()
            if pub:
                try:
                    date_str = datetime.fromisoformat(
                        pub.replace("Z", "+00:00")).isoformat()
                except Exception:
                    pass

            if not title or len(title) < 10:
                continue

            items.append(make_item(
                title, link, summary, date_str,
                source_label, country, flag, category, tags
            ))

        time.sleep(1)
    except Exception as exc:
        print(f"        ⚠  GNews error for '{source_label}': {exc}")
    return items

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"🔍  Regulatory feed fetch — {now_iso()}\n")
    all_items: list[dict] = []

    # 1. All RSS feeds
    for cfg in RSS_FEEDS:
        print(f"  → [RSS] {cfg['source']}")
        items = fetch_rss(cfg)
        print(f"        {len(items)} items fetched")
        all_items.extend(items)

    # 2. FATF (scrape — no RSS)
    print("  → [SCRAPE] FATF")
    items = fetch_fatf()
    print(f"        {len(items)} items fetched")
    all_items.extend(items)

    # 3. OFAC (scrape — RSS retired Jan 2025)
    print("  → [SCRAPE] OFAC")
    items = fetch_ofac()
    print(f"        {len(items)} items fetched")
    all_items.extend(items)

    # 4. GNews — AML / financial crime news
    if GNEWS_KEY:
        print("  → [GNEWS] AML & Regulatory News")
        items = fetch_gnews(
            query="anti-money laundering AML regulation compliance 2025",
            source_label="News (AML)",
            flag="📰", country="MEDIA", category="News",
            tags=["AML", "Financial Crime"]
        )
        print(f"        {len(items)} items fetched")
        all_items.extend(items)

        print("  → [GNEWS] Sanctions & OFAC News")
        items = fetch_gnews(
            query="sanctions OFAC financial compliance enforcement 2025",
            source_label="News (Sanctions)",
            flag="📰", country="MEDIA", category="News",
            tags=["Sanctions"]
        )
        print(f"        {len(items)} items fetched")
        all_items.extend(items)
    else:
        print("  → [GNEWS] Skipped — GNEWS_API_KEY not set")

    # ── Deduplicate & sort ───────────────────────────────────────────────────
    seen  = set()
    deduped = []
    all_items.sort(key=lambda x: x.get("date", ""), reverse=True)

    for item in all_items:
        key = re.sub(r"\s+", " ", item["title"].lower().strip())[:80]
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)

    deduped = deduped[:MAX_TOTAL]

    # ── AML / UK summary stats ───────────────────────────────────────────────
    aml_count    = sum(1 for i in deduped if i.get("is_aml"))
    uk_aml_count = sum(1 for i in deduped if i.get("is_aml") and i.get("is_uk"))
    print(f"\n  AML-tagged: {aml_count} | UK AML: {uk_aml_count}")

    payload = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total":        len(deduped),
        "aml_count":    aml_count,
        "uk_aml_count": uk_aml_count,
        "items":        deduped,
    }

    os.makedirs("data", exist_ok=True)
    out_path = "data/regulatory-updates.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"✅  Saved {len(deduped)} items → {out_path}\n")


if __name__ == "__main__":
    main()
