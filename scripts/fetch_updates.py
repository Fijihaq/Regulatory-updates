#!/usr/bin/env python3
"""
Regulatory Intelligence Feed — fetch_updates.py
Fetches from 14 sources and saves to data/regulatory-updates.json

Sources:
  RSS    : FCA, HMRC, ICO, EBA, ESMA, SEC, FinCEN, BIS, MAS
  Scrape : FATF, OFAC, NCA, SFO
  GNews  : AML + Sanctions news (optional — requires GNEWS_API_KEY secret)

Each item is scored 0-100 for Top-5 selection. Score breakdown:
  Recency      : 0-40 pts  (today=40, yesterday=32, 2d=24, 3d=16, 4d=8, older=0)
  AML keywords : 0-25 pts  (up to 5 matched keywords x 5pts each)
  UK regulator : 0-15 pts  (FCA/HMRC/NCA/SFO/PSR/ICO = +15)
  Impact tier  : 0-10 pts  (enforcement/fine/penalty=10, guidance=6, consultation=3)
  Source trust : 0-10 pts  (official regulator=10, scrape=7, news=4)
  TOTAL MAX    : 100 pts
"""

import feedparser
import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# ── Config ──────────────────────────────────────────────────────────────────
MAX_PER_SOURCE = 10
MAX_TOTAL      = 100
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

# ── RSS Sources ──────────────────────────────────────────────────────────────
RSS_FEEDS = [
    {
        "source": "FCA", "url": "https://www.fca.org.uk/news/rss.xml",
        "country": "UK", "flag": "🇬🇧", "category": "Regulator",
        "tags": ["AML", "Financial Crime", "Conduct"], "trust": 10,
    },
    {
        "source": "HMRC",
        "url": "https://www.gov.uk/government/organisations/hm-revenue-customs.atom",
        "country": "UK", "flag": "🇬🇧", "category": "Regulator",
        "tags": ["AML", "Tax", "Financial Crime"], "trust": 10,
    },
    {
        "source": "ICO", "url": "https://ico.org.uk/about-the-ico/media-centre/rss/",
        "country": "UK", "flag": "🇬🇧", "category": "Regulator",
        "tags": ["Data", "Privacy"], "trust": 10,
    },
    {
        "source": "EBA", "url": "https://www.eba.europa.eu/rss.xml",
        "country": "EU", "flag": "🇪🇺", "category": "Regulator",
        "tags": ["AML", "Prudential", "Banking"], "trust": 10,
    },
    {
        "source": "ESMA", "url": "https://www.esma.europa.eu/rss.xml",
        "country": "EU", "flag": "🇪🇺", "category": "Regulator",
        "tags": ["Market Integrity", "Capital Markets"], "trust": 10,
    },
    {
        "source": "SEC", "url": "https://www.sec.gov/rss/news/press.rss",
        "country": "US", "flag": "🇺🇸", "category": "Regulator",
        "tags": ["Securities", "Enforcement"], "trust": 10,
    },
    {
        "source": "FinCEN",
        "url": "https://www.fincen.gov/news-room/rss.xml",
        "country": "US", "flag": "🇺🇸", "category": "Regulator",
        "tags": ["AML", "Financial Crime", "Sanctions"], "trust": 10,
    },
    {
        "source": "BIS", "url": "https://www.bis.org/rss/index.htm",
        "country": "GLOBAL", "flag": "🌍", "category": "Regulator",
        "tags": ["Prudential", "Basel"], "trust": 10,
    },
    {
        "source": "MAS", "url": "https://www.mas.gov.sg/rss/news",
        "country": "SG", "flag": "🇸🇬", "category": "Regulator",
        "tags": ["AML", "Crypto", "Fintech"], "trust": 10,
    },
]

# ── Scoring Config ───────────────────────────────────────────────────────────
AML_KEYWORDS = {
    # Core AML — high value
    "anti-money laundering": 5, "money laundering": 5, "aml": 5,
    "financial crime": 4, "suspicious activity": 5, "sar": 4,
    "suspicious transaction": 5, "str": 3,
    # CDD / KYC
    "know your customer": 5, "kyc": 4, "customer due diligence": 5, "cdd": 4,
    "enhanced due diligence": 5, "edd": 4,
    # Ownership / PEP
    "beneficial ownership": 5, "beneficial owner": 5,
    "politically exposed": 4, "pep": 3,
    # Terrorism finance
    "terrorist financing": 5, "counter-terrorism": 4, "ctf": 3,
    # Proceeds / methods
    "proceeds of crime": 5, "money mule": 5, "layering": 4,
    "placement": 3, "integration": 3, "illicit finance": 4,
    # Reporting / intelligence
    "financial intelligence": 4, "unexplained wealth": 5,
    "unexplained wealth order": 5, "uwo": 4,
    # Crypto AML
    "crypto aml": 5, "virtual asset": 4, "vasp": 4,
    "travel rule": 4, "crypto compliance": 3,
}

IMPACT_KEYWORDS = {
    # Enforcement — highest impact
    "fine": 10, "penalty": 10, "enforcement": 10, "sanction": 9,
    "prosecution": 10, "convicted": 10, "sentenced": 10,
    "action against": 9, "banned": 9, "prohibited": 8,
    # Guidance / policy — medium
    "guidance": 6, "guideline": 6, "policy": 5, "framework": 5,
    "regulation": 5, "directive": 6, "rule": 4,
    # Consultation / review — lower
    "consultation": 3, "discussion paper": 3, "review": 3,
    "call for evidence": 3, "feedback": 2,
}

UK_SOURCES = {"FCA", "HMRC", "NCA", "SFO", "PSR", "ICO", "TPR", "HM Treasury"}

# ── Helpers ──────────────────────────────────────────────────────────────────

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

def parse_date_text(text: str) -> str:
    """Try to parse various date text formats into ISO."""
    text = text.strip()
    formats = [
        "%d %B %Y", "%B %d, %Y", "%d/%m/%Y", "%m/%d/%Y",
        "%Y-%m-%d", "%d-%m-%Y", "%b %d, %Y", "%d %b %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return now_iso()

def score_item(title: str, summary: str, source: str, date_iso: str, trust: int = 10) -> dict:
    """
    Score an item 0-100. Returns score + breakdown for transparency.
    """
    text = (title + " " + summary).lower()
    now  = datetime.now(timezone.utc)

    # 1. Recency score (0-40)
    recency_score = 0
    recency_label = "older"
    try:
        pub = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        age_days = (now - pub).days
        if age_days == 0:
            recency_score, recency_label = 40, "today"
        elif age_days == 1:
            recency_score, recency_label = 32, "yesterday"
        elif age_days == 2:
            recency_score, recency_label = 24, "2 days ago"
        elif age_days == 3:
            recency_score, recency_label = 16, "3 days ago"
        elif age_days <= 7:
            recency_score, recency_label = 8, "this week"
        elif age_days <= 14:
            recency_score, recency_label = 4, "last 2 weeks"
    except Exception:
        pass

    # 2. AML keyword score (0-25, capped)
    aml_hits = {}
    for kw, pts in AML_KEYWORDS.items():
        if kw in text and kw not in aml_hits:
            aml_hits[kw] = pts
    aml_score = min(25, sum(aml_hits.values()))
    is_aml = aml_score >= 4

    # 3. UK regulator score (0-15)
    uk_score = 15 if source in UK_SOURCES else 0
    is_uk = source in UK_SOURCES

    # 4. Impact tier score (0-10)
    impact_score = 0
    impact_label = "informational"
    for kw, pts in IMPACT_KEYWORDS.items():
        if kw in text:
            if pts > impact_score:
                impact_score = pts
                impact_label = kw

    # 5. Source trust score (0-10)
    trust_score = trust

    total = recency_score + aml_score + uk_score + impact_score + trust_score

    return {
        "score": total,
        "score_breakdown": {
            "recency":  recency_score,
            "recency_label": recency_label,
            "aml":      aml_score,
            "aml_keywords": list(aml_hits.keys())[:5],
            "uk_bonus": uk_score,
            "impact":   impact_score,
            "impact_label": impact_label,
            "trust":    trust_score,
        },
        "is_aml": is_aml,
        "is_uk":  is_uk,
    }

def make_item(title, link, summary, date, source, country, flag,
              category="Regulator", tags=None, trust=10) -> dict:
    clean_summary = strip_html(summary)[:400]
    scoring = score_item(title, clean_summary, source, date, trust)
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
        **scoring,
    }

# ── 1. RSS Fetcher ───────────────────────────────────────────────────────────

def fetch_rss(cfg: dict) -> list:
    items = []
    try:
        parsed = feedparser.parse(
            cfg["url"],
            request_headers={"User-Agent": HEADERS["User-Agent"]}
        )
        for entry in parsed.entries[:MAX_PER_SOURCE]:
            title   = (entry.get("title") or "").strip()
            link    = (entry.get("link") or "").strip()
            summary = entry.get("summary", entry.get("description", ""))
            if not title or len(title) < 10:
                continue
            items.append(make_item(
                title, link, summary, safe_rss_date(entry),
                cfg["source"], cfg["country"], cfg["flag"],
                cfg.get("category", "Regulator"), cfg.get("tags", []),
                cfg.get("trust", 10)
            ))
    except Exception as e:
        print(f"        ⚠  RSS error for {cfg['source']}: {e}")
    return items

# ── 2. NCA Scraper ───────────────────────────────────────────────────────────

def fetch_nca() -> list:
    """NCA press releases — scrape the live 'All news' listing.

    The site is Joomla-based; there is no <time> tag on listing items,
    so the date is extracted via regex from the surrounding text
    (format on-site is e.g. "09 June 2026").
    """
    items = []
    url = "https://www.nationalcrimeagency.gov.uk/news/all-news"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        main = soup.find("main") or soup

        seen_links = set()
        for a in main.find_all("a", href=True):
            href = a["href"]
            if "/news/" not in href:
                continue
            title = a.get_text(strip=True)
            # Filters out nav/breadcrumb links like "News" / "All news"
            if not title or len(title) < 15:
                continue

            link = href if href.startswith("http") else f"https://www.nationalcrimeagency.gov.uk{href}"
            if link in seen_links:
                continue
            seen_links.add(link)

            # Date sits as plain text near the headline, e.g. "09 June 2026"
            block = a.find_parent(["div", "li", "article"]) or a
            block_text = block.get_text(separator=" ", strip=True)
            date_str = now_iso()
            m = re.search(
                r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|"
                r"August|September|October|November|December)\s+(\d{4})\b",
                block_text)
            if m:
                date_str = parse_date_text(m.group())

            p = block.find("p") if hasattr(block, "find") else None
            summary = p.get_text(strip=True) if p else ""

            items.append(make_item(title, link, summary, date_str,
                                   "NCA", "UK", "🇬🇧", "Regulator",
                                   ["AML", "Financial Crime", "SAR"], 10))
            if len(items) >= MAX_PER_SOURCE:
                break
    except Exception as e:
        print(f"        ⚠  NCA scrape error: {e}")
    return items

# ── 3. SFO Scraper ───────────────────────────────────────────────────────────

def fetch_sfo() -> list:
    """SFO press releases."""
    items = []
    urls = [
        "https://www.sfo.gov.uk/press-room/press-releases/",
        "https://www.sfo.gov.uk/news/",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            cards = (
                soup.select("article.post")
                or soup.select("div.press-release")
                or soup.select("div.news-item")
                or soup.select("li.post")
            )

            if not cards:
                main = soup.find("main") or soup
                for a in main.find_all("a", href=True)[:MAX_PER_SOURCE]:
                    title = a.get_text(strip=True)
                    if len(title) < 20:
                        continue
                    href = a["href"]
                    link = href if href.startswith("http") else f"https://www.sfo.gov.uk{href}"
                    items.append(make_item(title, link, "", now_iso(),
                                           "SFO", "UK", "🇬🇧", "Regulator",
                                           ["AML", "Fraud", "Enforcement"], 10))
                break

            for card in cards[:MAX_PER_SOURCE]:
                a = card.find("a", href=True)
                if not a:
                    continue
                title = a.get_text(strip=True)
                if not title or len(title) < 15:
                    continue
                href = a["href"]
                link = href if href.startswith("http") else f"https://www.sfo.gov.uk{href}"

                date_str = now_iso()
                time_tag = card.find("time")
                if time_tag:
                    try:
                        date_str = datetime.fromisoformat(
                            time_tag.get("datetime", "").replace("Z", "+00:00")).isoformat()
                    except Exception:
                        raw = time_tag.get_text(strip=True)
                        date_str = parse_date_text(raw)

                p = card.find("p")
                summary = p.get_text(strip=True) if p else ""

                items.append(make_item(title, link, summary, date_str,
                                       "SFO", "UK", "🇬🇧", "Regulator",
                                       ["AML", "Fraud", "Enforcement"], 10))
            if items:
                break
        except Exception as e:
            print(f"        ⚠  SFO scrape error ({url}): {e}")
    return items

# ── 4. FATF Scraper ──────────────────────────────────────────────────────────

def fetch_fatf() -> list:
    items = []
    urls = [
        "https://www.fatf-gafi.org/en/the-fatf/news.html",
        "https://www.fatf-gafi.org/en/publications.html",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            selectors = [
                "div.search-result-item", "div.publication-item",
                "article.card", "div.content-listing__item",
                "li.publication", "div[class*='card']",
            ]
            cards = []
            for sel in selectors:
                cards = soup.select(sel)
                if len(cards) >= 3:
                    break

            if len(cards) < 3:
                main = soup.find("main") or soup
                for a in main.find_all("a", href=True)[:MAX_PER_SOURCE]:
                    t = a.get_text(strip=True)
                    if len(t) < 20:
                        continue
                    href = a["href"]
                    link = href if href.startswith("http") else f"https://www.fatf-gafi.org{href}"
                    items.append(make_item(t, link, "", now_iso(),
                                           "FATF", "GLOBAL", "🌍", "Regulator",
                                           ["AML", "Financial Crime"], 10))
                if items:
                    break
                continue

            for card in cards[:MAX_PER_SOURCE]:
                a = card.find("a", href=True)
                if not a:
                    continue
                title = a.get_text(strip=True)
                if len(title) < 15:
                    title = card.get_text(separator=" ", strip=True)[:120]
                href = a["href"]
                link = href if href.startswith("http") else f"https://www.fatf-gafi.org{href}"

                p = card.find("p")
                summary = p.get_text(strip=True) if p else ""

                date_str = now_iso()
                time_tag = card.find("time")
                if time_tag:
                    try:
                        date_str = datetime.fromisoformat(
                            time_tag.get("datetime", "").replace("Z", "+00:00")).isoformat()
                    except Exception:
                        date_str = parse_date_text(time_tag.get_text(strip=True))
                else:
                    m = re.search(
                        r"\b(\d{1,2})\s+(January|February|March|April|May|June|"
                        r"July|August|September|October|November|December)\s+(\d{4})\b",
                        card.get_text()
                    )
                    if m:
                        date_str = parse_date_text(m.group())

                if title:
                    items.append(make_item(title, link, summary, date_str,
                                           "FATF", "GLOBAL", "🌍", "Regulator",
                                           ["AML", "Financial Crime"], 10))
            if items:
                break
        except Exception as e:
            print(f"        ⚠  FATF scrape error: {e}")
    return items

# ── 5. OFAC Scraper ──────────────────────────────────────────────────────────

def fetch_ofac() -> list:
    items = []
    url = "https://ofac.treasury.gov/recent-actions"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        selectors = [
            "table tbody tr", "div.view-content div.views-row",
            "ul.view-content li", "div.item-list li", "article",
        ]
        rows = []
        for sel in selectors:
            rows = soup.select(sel)
            if len(rows) >= 2:
                break

        if len(rows) < 2:
            main = soup.find("main") or soup
            for a in main.find_all("a", href=True)[:MAX_PER_SOURCE]:
                t = a.get_text(strip=True)
                if len(t) < 10:
                    continue
                href = a["href"]
                link = href if href.startswith("http") else f"https://ofac.treasury.gov{href}"
                items.append(make_item(t, link, "", now_iso(),
                                       "OFAC", "US", "🇺🇸", "Sanctions",
                                       ["Sanctions"], 10))
            return items

        for row in rows[:MAX_PER_SOURCE]:
            a = row.find("a", href=True)
            if not a:
                continue
            title = a.get_text(strip=True)
            if not title or len(title) < 5:
                continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://ofac.treasury.gov{href}"

            date_str = now_iso()
            rt = row.get_text(separator=" ")
            m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", rt)
            if m:
                try:
                    date_str = datetime.strptime(m.group(), "%m/%d/%Y").replace(
                        tzinfo=timezone.utc).isoformat()
                except Exception:
                    pass
            else:
                m2 = re.search(
                    r"\b(January|February|March|April|May|June|July|August|"
                    r"September|October|November|December)\s+\d{1,2},\s+\d{4}\b", rt)
                if m2:
                    date_str = parse_date_text(m2.group())

            tds = row.find_all("td")
            summary = tds[-1].get_text(strip=True) if len(tds) >= 2 else ""

            items.append(make_item(title, link, summary, date_str,
                                   "OFAC", "US", "🇺🇸", "Sanctions",
                                   ["Sanctions"], 10))
    except Exception as e:
        print(f"        ⚠  OFAC scrape error: {e}")
    return items

# ── 6. GNews ─────────────────────────────────────────────────────────────────

def fetch_gnews(query, label, flag, country, category, tags, trust=4) -> list:
    if not GNEWS_KEY:
        return []
    items = []
    try:
        resp = requests.get(
            "https://gnews.io/api/v4/search",
            params={"q": query, "lang": "en", "max": MAX_PER_SOURCE,
                    "apikey": GNEWS_KEY, "sortby": "publishedAt"},
            timeout=TIMEOUT
        )
        resp.raise_for_status()
        for art in resp.json().get("articles", []):
            title   = (art.get("title") or "").strip()
            link    = (art.get("url") or "").strip()
            summary = (art.get("description") or "").strip()
            pub     = art.get("publishedAt", "")
            date_str = now_iso()
            if pub:
                try:
                    date_str = datetime.fromisoformat(pub.replace("Z", "+00:00")).isoformat()
                except Exception:
                    pass
            if not title or len(title) < 10:
                continue
            items.append(make_item(title, link, summary, date_str,
                                   label, country, flag, category, tags, trust))
        time.sleep(1)
    except Exception as e:
        print(f"        ⚠  GNews error '{label}': {e}")
    return items

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"🔍  Regulatory feed fetch — {now_iso()}\n")
    all_items = []

    # RSS feeds
    for cfg in RSS_FEEDS:
        print(f"  → [RSS]    {cfg['source']}")
        items = fetch_rss(cfg)
        print(f"             {len(items)} items")
        all_items.extend(items)

    # Scraped sources
    for label, fn in [("NCA", fetch_nca), ("SFO", fetch_sfo),
                      ("FATF", fetch_fatf), ("OFAC", fetch_ofac)]:
        print(f"  → [SCRAPE] {label}")
        items = fn()
        print(f"             {len(items)} items")
        all_items.extend(items)

    # GNews
    if GNEWS_KEY:
        for query, label, tags in [
            ("anti-money laundering AML compliance regulation 2025",
             "AML News", ["AML", "Financial Crime"]),
            ("sanctions OFAC compliance financial enforcement 2025",
             "Sanctions News", ["Sanctions"]),
        ]:
            print(f"  → [GNEWS]  {label}")
            items = fetch_gnews(query, label, "📰", "MEDIA", "News", tags, trust=4)
            print(f"             {len(items)} items")
            all_items.extend(items)
    else:
        print("  → [GNEWS]  Skipped — GNEWS_API_KEY not set")

    # Deduplicate by title
    seen, deduped = set(), []
    all_items.sort(key=lambda x: x.get("date", ""), reverse=True)
    for item in all_items:
        key = re.sub(r"\s+", " ", item["title"].lower().strip())[:80]
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    deduped = deduped[:MAX_TOTAL]

    # ── Score and rank for Top-5 picks ───────────────────────────────────────
    sorted_by_score = sorted(deduped, key=lambda x: x.get("score", 0), reverse=True)

    # Top 5 overall
    top5_overall = sorted_by_score[:5]

    # Top 5 UK AML — must have is_uk=True and is_aml=True, sorted by score
    uk_aml = [i for i in sorted_by_score if i.get("is_uk") and i.get("is_aml")]
    top5_uk_aml = uk_aml[:5]

    # Stats
    aml_count    = sum(1 for i in deduped if i.get("is_aml"))
    uk_aml_count = len(uk_aml)

    print(f"\n  Total items : {len(deduped)}")
    print(f"  AML-tagged  : {aml_count}")
    print(f"  UK AML      : {uk_aml_count}")
    print(f"\n  Top 5 overall (by score):")
    for i, item in enumerate(top5_overall, 1):
        b = item["score_breakdown"]
        print(f"    {i}. [{item['score']}pts] {item['source']} — {item['title'][:70]}")
        print(f"       recency={b['recency']} aml={b['aml']} uk={b['uk_bonus']} impact={b['impact']} trust={b['trust']}")

    print(f"\n  Top 5 UK AML:")
    for i, item in enumerate(top5_uk_aml, 1):
        print(f"    {i}. [{item['score']}pts] {item['source']} — {item['title'][:70]}")

    # Build payload
    payload = {
        "last_updated":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total":         len(deduped),
        "aml_count":     aml_count,
        "uk_aml_count":  uk_aml_count,
        "top5_overall":  top5_overall,
        "top5_uk_aml":   top5_uk_aml,
        "items":         deduped,
        "scoring_rules": {
            "recency_max": 40, "aml_max": 25, "uk_bonus": 15,
            "impact_max": 10, "trust_max": 10, "total_max": 100,
            "description": (
                "Each item scored 0-100. Recency (0-40): today=40, yesterday=32, "
                "2d=24, 3d=16, 7d=8, 14d=4. AML keywords (0-25): matched terms "
                "from 30-word AML lexicon, capped at 25. UK regulator bonus (0-15): "
                "+15 for FCA/HMRC/NCA/SFO/PSR/ICO/TPR. Impact tier (0-10): "
                "enforcement/fine=10, guidance=6, consultation=3. "
                "Source trust (0-10): official regulator RSS=10, scrape=7, news=4."
            )
        }
    }

    os.makedirs("data", exist_ok=True)
    with open("data/regulatory-updates.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\n✅  Saved {len(deduped)} items → data/regulatory-updates.json\n")


if __name__ == "__main__":
    main()
