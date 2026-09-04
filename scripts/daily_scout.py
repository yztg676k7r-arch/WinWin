#!/usr/bin/env python3
"""Win Win Daily Scout.

Conservative, no-secret crawler for official German contest pages. It scans due
sources, discovers fresh candidate pages, rejects known policy violations and
adds only high-confidence matches to contests.json. Ambiguous finds are written
to data/daily-scout-review.json instead of being published.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONTESTS_FILE = ROOT / "contests.json"
SOURCES_FILE = ROOT / "sources.json"
REPORT_FILE = ROOT / "data" / "daily-scout-report.json"
REVIEW_FILE = ROOT / "data" / "daily-scout-review.json"
TODAY = date.today()
MAX_SOURCES = int(os.getenv("WINWIN_SCOUT_MAX_SOURCES", "80"))
MAX_NEW = int(os.getenv("WINWIN_SCOUT_MAX_NEW", "12"))
TIMEOUT = 14
UA = "WinWin-Daily-Scout/8.4 (+https://github.com/yztg676k7r-arch/WinWin)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.5"})

KEYWORDS = ("gewinnspiel", "verlosung", "gewinnen", "giveaway", "win")
DISCOVERY_PATHS = ("/gewinnspiele", "/gewinnspiel", "/aktionen", "/aktion")
AGGREGATORS = {
    "gewinnspiele.de", "gewinnspielmarkt.de", "gewinnspielverzeichnis.de",
    "gewinnspiel-portal.de", "gewinnspiele-markt.de", "kostenlose-gewinnspiele.de",
}

# A page is auto-published only if none of these strong exclusion signals occur.
REJECT_RULES = {
    "purchase": [
        r"kassenbon", r"kaufbeleg", r"bon hochladen", r"rechnung hochladen",
        r"produkt(?:e)? kaufen", r"kauf(?:es|s)?\s+(?:ist\s+)?(?:erforderlich|notwendig|voraussetzung)",
        r"mindestbestellwert", r"aktionsprodukt(?:e)?",
    ],
    "paid": [
        r"premium[- ]?sms", r"0137\d", r"0900\d", r"kostenpflichtig(?:e|er)?\s+(?:anruf|sms|teilnahme)",
        r"los(?:e)? kaufen", r"loseinsatz", r"spieleinsatz", r"kostenpflichtig(?:es|en)?\s+abo",
    ],
    "club": [
        r"nur f[uü]r (?:club)?mitglieder", r"mitgliedschaft\s+(?:ist\s+)?erforderlich",
        r"kostenpflichtige mitgliedschaft",
    ],
}

FORM_SIGNALS = (
    "teilnahmeformular", "jetzt teilnehmen", "am gewinnspiel teilnehmen",
    "name=\"email\"", "name='email'", "type=\"email\"", "type='email'", "mailto:",
)
SOCIAL_WORDS = ("instagram", "facebook", "tiktok")
SOCIAL_ACTIONS = ("folgen", "kommentieren", "liken", "markieren", "teilen", "follow", "comment")

DATE_PATTERNS = [
    r"(?:teilnahmeschluss|aktionsende|einsendeschluss|läuft bis|laeuft bis|endet am|bis zum|bis)\s*[:\-]?\s*(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})",
    r"(?:teilnahmeschluss|aktionsende|einsendeschluss|läuft bis|laeuft bis|endet am|bis zum|bis)\s*[:\-]?\s*(\d{1,2}\.?\s+(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\s+\d{4})",
]
MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}

@dataclass
class Page:
    url: str
    text: str
    raw: str
    soup: BeautifulSoup


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def canonical_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
    q = [(k, v) for k, v in q if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    path = re.sub(r"/+", "/", p.path or "/")
    return urllib.parse.urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), path.rstrip("/") or "/", urllib.parse.urlencode(q), ""))


def domain_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def same_domain(url: str, domain: str) -> bool:
    d = domain_of(url)
    domain = domain.lower().removeprefix("www.")
    return d == domain or d.endswith("." + domain)


def fetch(url: str) -> Page | None:
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", "text/html"):
            return None
        if len(r.content) > 3_000_000:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = " ".join(soup.stripped_strings)
        return Page(canonical_url(r.url), text[:220_000], r.text[:500_000], soup)
    except requests.RequestException:
        return None


def page_links(page: Page, source_domain: str) -> set[str]:
    found = set()
    for a in page.soup.find_all("a", href=True):
        href = urllib.parse.urljoin(page.url, a.get("href"))
        if not href.startswith(("http://", "https://")) or not same_domain(href, source_domain):
            continue
        label = norm((a.get_text(" ", strip=True) or "") + " " + href)
        if any(k in label for k in KEYWORDS):
            found.add(canonical_url(href))
    return found


def sitemap_links(domain: str) -> set[str]:
    out = set()
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        try:
            r = SESSION.get(f"https://{domain}{path}", timeout=TIMEOUT)
            if r.status_code >= 400 or len(r.content) > 4_000_000:
                continue
            root = ET.fromstring(r.content)
            locs = [x.text.strip() for x in root.iter() if x.tag.endswith("loc") and x.text]
            # One level of sitemap index only; enough for daily discovery without hammering sites.
            nested = [u for u in locs[:20] if u.endswith(".xml")]
            urls = [u for u in locs if not u.endswith(".xml")]
            for sm in nested[:6]:
                try:
                    rr = SESSION.get(sm, timeout=TIMEOUT)
                    if rr.ok and len(rr.content) <= 4_000_000:
                        rt = ET.fromstring(rr.content)
                        urls += [x.text.strip() for x in rt.iter() if x.tag.endswith("loc") and x.text]
                except Exception:
                    pass
            for u in urls:
                n = norm(u)
                if same_domain(u, domain) and any(k in n for k in KEYWORDS):
                    out.add(canonical_url(u))
            if out:
                break
        except Exception:
            continue
    return out


def parse_deadline(text: str) -> date | None:
    low = text.lower()
    for pattern in DATE_PATTERNS:
        for m in re.finditer(pattern, low, flags=re.I):
            raw = m.group(1).strip().replace("/", ".").replace("-", ".")
            d = None
            mnum = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", raw)
            if mnum:
                y = int(mnum.group(3)); y += 2000 if y < 100 else 0
                try: d = date(y, int(mnum.group(2)), int(mnum.group(1)))
                except ValueError: pass
            if not d:
                mw = re.fullmatch(r"(\d{1,2})\.?\s+([a-zä]+)\s+(\d{4})", raw, flags=re.I)
                if mw and mw.group(2).lower() in MONTHS:
                    try: d = date(int(mw.group(3)), MONTHS[mw.group(2).lower()], int(mw.group(1)))
                    except ValueError: pass
            if d and TODAY <= d <= TODAY + timedelta(days=370):
                return d
    return None


def title_of(page: Page) -> str:
    h1 = page.soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    if not title and page.soup.title:
        title = page.soup.title.get_text(" ", strip=True)
    title = re.sub(r"\s+", " ", html.unescape(title)).strip()
    return title[:160]


def prize_of(page: Page, title: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+|\s{2,}", page.text)
    for s in sentences:
        ns = norm(s)
        if ("gewinnen" in ns or "verlosen" in ns or "zu gewinnen" in ns) and 15 <= len(s) <= 240:
            return re.sub(r"\s+", " ", s).strip()[:220]
    return title[:180] or "Gewinn laut Aktionsseite"


def winner_count(text: str) -> int | None:
    patterns = [r"(\d{1,3})\s*[x×]\s*", r"(\d{1,3})\s+gewinner(?:innen)?", r"unter\s+(\d{1,3})\s+gewinner"]
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 999: return n
    return None


def entry_type(page: Page) -> str | None:
    low = page.raw.lower()
    has_form = bool(page.soup.find("form")) or any(s in low for s in FORM_SIGNALS)
    has_email = "mailto:" in low
    social = any(x in norm(page.text) for x in SOCIAL_WORDS) and any(x in norm(page.text) for x in SOCIAL_ACTIONS)
    if has_form: return "form"
    if has_email: return "email"
    if social: return None
    return None


def rejection_reason(page: Page) -> str | None:
    t = norm(page.text)
    raw_low = page.text.lower()
    for reason, pats in REJECT_RULES.items():
        if any(re.search(p, raw_low, flags=re.I) for p in pats):
            return reason
    # Radio pages are acceptable only with a web/email entry route; call/SMS-only is rejected.
    if ("anrufen" in t or "hotline" in t or "sms" in t) and not entry_type(page):
        return "call-or-sms-only"
    # Social activity may be mentioned as bonus, but social-only pages never auto-publish.
    social = any(x in t for x in SOCIAL_WORDS) and any(x in t for x in SOCIAL_ACTIONS)
    if social and not entry_type(page):
        return "social-only"
    return None


def chance_score(winners: int | None, title: str, prize: str) -> int:
    score = 42
    if winners:
        score += min(30, 8 + int(8 * min(winners, 20) ** 0.45))
    value_text = norm(title + " " + prize)
    if any(x in value_text for x in ("auto", "iphone", "reise", "10000", "100.000", "haus")):
        score -= 12
    if any(x in value_text for x in ("buch", "ticket", "kinokarte", "produktpaket")):
        score += 8
    return max(20, min(88, score))


def source_due(s: dict) -> bool:
    if not s.get("active", True) or s.get("socialOnly") is True:
        return False
    last = s.get("lastChecked")
    interval = int(s.get("checkIntervalDays") or 3)
    if not last: return True
    try: return (TODAY - date.fromisoformat(last[:10])).days >= interval
    except Exception: return True


def source_rank(s: dict):
    p = {"high": 0, "medium": 1, "low": 2}.get(s.get("monitoringPriority"), 1)
    return (p, -int(s.get("quality") or 0), s.get("lastChecked") or "")


def candidate_urls(source: dict) -> set[str]:
    domain = source.get("domain", "").strip().lower()
    if not domain: return set()
    urls = set()
    # First ask likely landing pages; they often expose the exact participation URL.
    for path in ("/",) + DISCOVERY_PATHS:
        p = fetch(f"https://{domain}{path}")
        if p:
            urls |= page_links(p, domain)
        time.sleep(0.08)
    urls |= sitemap_links(domain)
    return set(list(urls)[:45])


def make_contest(source: dict, page: Page, deadline: date) -> dict:
    title = title_of(page)
    prize = prize_of(page, title)
    winners = winner_count(page.text)
    etype = entry_type(page) or "form"
    digest = hashlib.sha1((page.url + deadline.isoformat()).encode()).hexdigest()[:8]
    sid = source.get("id") or norm(source.get("name", "source")).replace(" ", "-")
    cid = f"{sid}-{deadline.strftime('%Y%m%d')}-{digest}"
    score = chance_score(winners, title, prize)
    priority = "hoch" if score >= 62 else "mittel" if score >= 43 else "niedrig"
    return {
        "id": cid,
        "title": title or f"Gewinnspiel von {source.get('name', source.get('domain'))}",
        "provider": source.get("name") or source.get("domain"),
        "prize": prize,
        "url": page.url,
        "category": (source.get("categories") or ["Sonstiges"])[0],
        "country": "Deutschland",
        "deadline": deadline.strftime("%d.%m.%Y"),
        "winners": winners,
        "new": True,
        "daily": bool(re.search(r"täglich|taeglich|jeden tag", page.text, re.I)),
        "international": False,
        "requirements": "Kostenlose Teilnahme über offizielles Web-Angebot",
        "purchaseRequired": False,
        "receiptRequired": False,
        "winnerKnown": bool(winners),
        "verified": TODAY.strftime("%d.%m.%Y"),
        "providerTrust": min(5, max(3, int(source.get("quality") or 4))),
        "effort": 1 if etype == "form" else 2,
        "entryType": etype,
        "multipleEntry": bool(re.search(r"mehrfach|täglich|taeglich|jeden tag", page.text, re.I)),
        "highValuePrize": any(x in norm(title + " " + prize) for x in ("auto", "reise", "iphone", "euro", "€")),
        "tags": ["Daily Scout", "kostenlos", "neu"],
        "addedAt": TODAY.strftime("%d.%m.%Y"),
        "sourceId": sid,
        "deEligibility": "bestätigt",
        "participationFrequency": "täglich" if re.search(r"täglich|taeglich|jeden tag", page.text, re.I) else "einmalig",
        "chanceScore": score,
        "priority": priority,
        "qualityScore": 94,
        "shortDescription": prize[:220],
        "dataCompleteness": 92 if winners is not None else 86,
        "lastVerified": TODAY.strftime("%d.%m.%Y"),
        "catalogStatus": "active",
        "scoutStatus": "verified",
        "scoutAdded": True,
    }


def discover_external() -> list[str]:
    """Small regional discovery pass via Bing RSS, no API key required.

    Results are never trusted blindly; every page still has to pass the same
    strict classifier. Aggregator domains are excluded.
    """
    queries = [
        f'Gewinnspiel Niedersachsen {TODAY.strftime("%B %Y")}',
        f'Gewinnspiel Hamburg {TODAY.strftime("%B %Y")}',
        f'Gewinnspiel Bremen {TODAY.strftime("%B %Y")}',
        f'Gewinnspiel Deutschland Teilnahmeschluss {TODAY.year}',
    ]
    urls = []
    for q in queries:
        try:
            u = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(q)
            r = SESSION.get(u, timeout=TIMEOUT)
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                link = item.findtext("link") or ""
                d = domain_of(link)
                if link.startswith("http") and d and not any(d == a or d.endswith("." + a) for a in AGGREGATORS):
                    urls.append(canonical_url(link))
        except Exception:
            pass
    # deterministic de-duplication, small load
    return list(dict.fromkeys(urls))[:25]


def main():
    contests_doc = load_json(CONTESTS_FILE)
    sources_doc = load_json(SOURCES_FILE)
    contests = contests_doc.get("contests") if isinstance(contests_doc, dict) else contests_doc
    sources = sources_doc.get("sources") if isinstance(sources_doc, dict) else sources_doc
    if not isinstance(contests, list) or not isinstance(sources, list):
        raise SystemExit("Unexpected catalog schema")

    existing_urls = {canonical_url(c.get("url", "")) for c in contests if c.get("url")}
    existing_ids = {str(c.get("id")) for c in contests}
    source_by_domain = {str(s.get("domain", "")).lower().removeprefix("www."): s for s in sources if s.get("domain")}
    due = sorted([s for s in sources if source_due(s)], key=source_rank)[:MAX_SOURCES]

    stats = {"checkedSources": 0, "candidatePages": 0, "published": 0, "review": 0, "rejected": 0, "expiredArchived": 0}
    review = []
    additions = []

    # Archive expired catalog entries without deleting IDs/history.
    for c in contests:
        raw = str(c.get("deadline", ""))
        try:
            d = datetime.strptime(raw, "%d.%m.%Y").date()
            if d < TODAY and c.get("catalogStatus") == "active":
                c["catalogStatus"] = "expired"
                stats["expiredArchived"] += 1
        except Exception:
            pass

    for source in due:
        stats["checkedSources"] += 1
        urls = candidate_urls(source)
        source["lastChecked"] = TODAY.isoformat()
        source["lastCatalogReview"] = TODAY.isoformat()
        source["lastResultCount"] = len(urls)
        source["emptyChecks"] = int(source.get("emptyChecks") or 0) + (1 if not urls else 0)
        source["successfulChecks"] = int(source.get("successfulChecks") or 0) + (1 if urls else 0)
        for url in urls:
            if len(additions) >= MAX_NEW or url in existing_urls:
                continue
            stats["candidatePages"] += 1
            page = fetch(url)
            if not page: continue
            reason = rejection_reason(page)
            deadline = parse_deadline(page.text)
            etype = entry_type(page)
            title = title_of(page)
            if reason:
                stats["rejected"] += 1
                review.append({"url": page.url, "sourceId": source.get("id"), "title": title, "status": "rejected", "reason": reason})
                continue
            if not deadline or not etype or not any(k in norm(title + " " + page.text[:5000]) for k in ("gewinnspiel", "verlosung", "gewinnen")):
                stats["review"] += 1
                review.append({"url": page.url, "sourceId": source.get("id"), "title": title, "status": "review", "reason": "deadline-or-entry-route-not-unambiguous"})
                continue
            item = make_contest(source, page, deadline)
            if item["id"] not in existing_ids:
                additions.append(item); existing_ids.add(item["id"]); existing_urls.add(page.url)

    # Discovery outside the existing source catalog. Only pages that pass all
    # strict checks can publish; otherwise they merely appear in review.
    for url in discover_external():
        if len(additions) >= MAX_NEW or url in existing_urls:
            continue
        page = fetch(url)
        if not page: continue
        d = domain_of(page.url)
        source = source_by_domain.get(d)
        if not source:
            source = {
                "id": "scout-" + re.sub(r"[^a-z0-9]+", "-", d)[:48].strip("-"),
                "name": d,
                "domain": d,
                "country": "Deutschland",
                "countriesAllowed": ["Deutschland"],
                "type": "Daily-Scout-Fund",
                "categories": ["Regional", "Freizeit"],
                "automation": "yellow",
                "quality": 4,
                "active": True,
                "requiresLogin": False,
                "socialOnly": False,
                "checkIntervalDays": 2,
                "lastChecked": TODAY.isoformat(),
                "notes": "Vom Daily Scout entdeckt; automatische Aufnahme nur bei eindeutigem kostenlosen Web-Gewinnspiel.",
                "germanyEligibility": "yes",
                "verification": "candidate",
                "monitoringPriority": "high" if any(x in norm(page.text[:3000]) for x in ("niedersachsen", "hamburg", "bremen")) else "medium",
                "policyReview": "daily-scout-strict",
            }
        reason = rejection_reason(page)
        deadline = parse_deadline(page.text)
        etype = entry_type(page)
        title = title_of(page)
        if reason or not deadline or not etype or not any(k in norm(title + " " + page.text[:5000]) for k in ("gewinnspiel", "verlosung", "gewinnen")):
            review.append({"url": page.url, "sourceId": source.get("id"), "title": title, "status": "rejected" if reason else "review", "reason": reason or "insufficient-confidence"})
            continue
        if d not in source_by_domain:
            sources.append(source); source_by_domain[d] = source
        item = make_contest(source, page, deadline)
        if item["id"] not in existing_ids:
            additions.append(item); existing_ids.add(item["id"]); existing_urls.add(page.url)

    contests.extend(additions)
    stats["published"] = len(additions)
    stats["review"] = sum(1 for x in review if x["status"] == "review")
    stats["rejected"] = sum(1 for x in review if x["status"] == "rejected")

    if isinstance(contests_doc, dict):
        contests_doc["contests"] = contests
        contests_doc["updated"] = TODAY.isoformat()
        contests_doc["dailyScout"] = {"lastRun": datetime.now().astimezone().isoformat(timespec="seconds"), **stats}
        contests_doc["activeCountAtRelease"] = sum(1 for c in contests if c.get("catalogStatus") == "active")
    if isinstance(sources_doc, dict):
        sources_doc["sources"] = sources
        sources_doc["updated"] = TODAY.isoformat()
        sources_doc["dailyScoutLastRun"] = datetime.now().astimezone().isoformat(timespec="seconds")

    write_json(CONTESTS_FILE, contests_doc)
    write_json(SOURCES_FILE, sources_doc)
    write_json(REVIEW_FILE, {"date": TODAY.isoformat(), "items": review[:250]})
    write_json(REPORT_FILE, {"date": TODAY.isoformat(), **stats, "newIds": [x["id"] for x in additions]})
    print(json.dumps({"ok": True, **stats, "newIds": [x["id"] for x in additions]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
