#!/usr/bin/env python3
"""One-off bulk discovery for WinWin on 2026-09-30.

Discovers current contests from GewinnHai only as a discovery index, follows the
redirect to the organizer's direct page, and publishes at most TARGET new
contests after policy checks on the organizer page itself.

Important: local user state is never touched; only catalog/source metadata is
updated.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONTESTS_FILE = ROOT / "contests.json"
SOURCES_FILE = ROOT / "sources.json"
VERSION_FILE = ROOT / "version.json"
REPORT_FILE = ROOT / "data" / "bulk-discovery-2026-09-30.json"

TODAY = date(2026, 9, 30)
TARGET = int(os.getenv("WINWIN_BULK_TARGET", "50"))
MAX_DETAILS = int(os.getenv("WINWIN_BULK_MAX_DETAILS", "360"))
TIME_BUDGET = int(os.getenv("WINWIN_BULK_BUDGET_SECONDS", "1500"))
STOP_AT = time.monotonic() + TIME_BUDGET
TIMEOUT = 12

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "WinWin-Bulk-Discovery/8.9 (+https://github.com/yztg676k7r-arch/WinWin)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.4",
})

INDEX_PAGES = (
    "https://www.gewinnhai.de/gewinnspiele/oktober-2026",
    "https://www.gewinnhai.de/gewinnspiele/oktober-2026?page=2",
    "https://www.gewinnhai.de/gewinnspiele/oktober-2026?page=3",
    "https://www.gewinnhai.de/gewinnspiele/oktober-2026?page=4",
    "https://www.gewinnhai.de/gewinnspiele/oktober-2026?page=5",
    "https://www.gewinnhai.de/gewinnspiele/oktober-2026?page=6",
    "https://www.gewinnhai.de/gewinnspiele/oktober-2026?page=7",
    "https://www.gewinnhai.de/gewinnspiele/november-2026",
    "https://www.gewinnhai.de/gewinnspiele/november-2026?page=2",
    "https://www.gewinnhai.de/gewinnspiele/dezember-2026",
    "https://www.gewinnhai.de/gewinnspiele/dezember-2026?page=2",
    "https://www.gewinnhai.de/gewinnspiele/januar-2027",
)

# Broaden the pool after the near-term month pages. /neu catches the freshest
# discoveries; generic pages cover contests without a reliable month index.
INDEX_PAGES = INDEX_PAGES + ("https://www.gewinnhai.de/neu",) + tuple(
    f"https://www.gewinnhai.de/gewinnspiele?page={i}" for i in range(1, 21)
)

SOCIAL_HOSTS = {
    "instagram.com", "www.instagram.com", "facebook.com", "www.facebook.com",
    "tiktok.com", "www.tiktok.com", "x.com", "twitter.com",
}

MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "dezember": 12,
}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def canonical_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
    q = [(k, v) for k, v in q if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    path = re.sub(r"/+", "/", p.path or "/").rstrip("/") or "/"
    return urllib.parse.urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), path, urllib.parse.urlencode(q), ""))


def domain(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def get(url: str):
    if time.monotonic() >= STOP_AT:
        return None
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return None
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return None
        if len(r.content) > 4_000_000:
            return None
        return r
    except requests.RequestException:
        return None


def soup_and_text(response):
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = " ".join(soup.stripped_strings)
    return soup, text[:300_000]


def collect_detail_urls() -> list[str]:
    found = []
    for page in INDEX_PAGES:
        if time.monotonic() >= STOP_AT:
            break
        r = get(page)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urllib.parse.urljoin(r.url, a.get("href"))
            p = urllib.parse.urlsplit(href)
            if p.netloc.endswith("gewinnhai.de") and p.path.startswith("/gewinnspiel/") and p.path.count("/") >= 2:
                found.append(canonical_url(href))
        time.sleep(0.08)
    # preserve discovery order and deduplicate
    return list(dict.fromkeys(found))[:MAX_DETAILS]


def strings_after_label(soup: BeautifulSoup, label: str, stop_labels: tuple[str, ...], max_items: int = 8) -> list[str]:
    vals = list(soup.stripped_strings)
    nlabel = norm(label)
    for i, s in enumerate(vals):
        if norm(s) == nlabel:
            out = []
            for x in vals[i + 1:]:
                nx = norm(x)
                if any(nx == norm(stop) for stop in stop_labels):
                    break
                if x and x not in out:
                    out.append(x)
                if len(out) >= max_items:
                    break
            return out
    return []


def parse_german_date(raw: str) -> date | None:
    raw0 = re.sub(r"\s+", " ", raw.strip().lower())
    miso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw0)
    if miso:
        try:
            return date(int(miso.group(1)), int(miso.group(2)), int(miso.group(3)))
        except ValueError:
            return None
    raw = raw0.replace("/", ".").replace("-", ".")
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", raw)
    if m:
        y = int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})\.?\s+([a-zä]+)\s+(\d{4})", raw)
    if m and m.group(2) in MONTHS:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    return None


DATE_TOKEN = r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\.?\s+(?:Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+\d{4})"
DEADLINE_PATTERNS = [
    rf"(?:teilnahmeschluss|einsendeschluss|aktionsende|teilnahmefrist)\s*[:\-]?\s*(?:am\s*)?{DATE_TOKEN}",
    rf"(?:gewinnspiel|aktion|teilnahme|aktionszeitraum|teilnahmezeitraum).{{0,180}}?(?:endet\s*(?:am)?|läuft\s*(?:bis|bis zum)?|laeuft\s*(?:bis|bis zum)?|bis\s*(?:zum|einschließlich|einschliesslich)?|ende\s*[:\-]?)\s*(?:am\s*)?{DATE_TOKEN}",
]


def official_deadline(text: str) -> date | None:
    candidates = []
    for pat in DEADLINE_PATTERNS:
        for m in re.finditer(pat, text, flags=re.I | re.S):
            raw = m.group(1)
            d = parse_german_date(raw)
            if d and TODAY + timedelta(days=1) <= d <= TODAY + timedelta(days=370):
                candidates.append(d)
    if not candidates:
        return None
    # Prefer the nearest future deadline; explicit patterns prevent event dates from
    # dominating in most cases.
    return min(candidates)


def has_germany_eligibility(text: str) -> bool:
    low = text.lower()
    checks = [
        r"(?:wohnsitz|wohnhaft|wohnort).{0,120}(?:deutschland|bundesrepublik)",
        r"(?:teilnahmeberechtigt|teilnehmen können|teilnehmen duerfen|teilnehmen dürfen).{0,180}(?:deutschland|bundesrepublik)",
        r"(?:deutschland|bundesrepublik).{0,120}(?:wohnsitz|wohnhaft|teilnahmeberechtigt)",
    ]
    return any(re.search(p, low, flags=re.I | re.S) for p in checks)


def purchase_required(text: str) -> bool:
    low = text.lower()
    # Remove common explicit negations before testing strong positive signals.
    masked = re.sub(r"(?:kein(?:e|en|er)?|ohne)\s+(?:produkt)?kauf\s+(?:ist\s+)?(?:notwendig|erforderlich|voraussetzung)", "", low)
    masked = re.sub(r"(?:kein(?:e|en|er)?|ohne)\s+(?:kassenbon|kaufbeleg|bon)\s+(?:ist\s+)?(?:notwendig|erforderlich)", "", masked)
    strong = [
        r"kassenbon\s+(?:hochladen|einsenden|fotografieren)",
        r"kaufbeleg\s+(?:hochladen|einsenden|fotografieren)",
        r"bon\s+(?:hochladen|einsenden|fotografieren)",
        r"rechnung\s+hochladen",
        r"mindestbestellwert",
        r"aktionsprodukt(?:e)?\s+(?:kaufen|erwerben)",
        r"(?:produkt|ware|artikel).{0,40}\bkaufen\b.{0,80}(?:teilnahme|gewinnspiel)",
        r"\bkauf\b.{0,60}(?:voraussetzung|erforderlich|notwendig|pflicht)",
        r"(?:teilnahme|gewinnspiel).{0,80}\bkauf\b.{0,40}(?:voraussetzung|erforderlich|notwendig|pflicht)",
    ]
    return any(re.search(p, masked, flags=re.I | re.S) for p in strong)


def club_required(text: str) -> bool:
    low = text.lower()
    return bool(re.search(
        r"(?:nur|ausschließlich|ausschliesslich).{0,40}(?:club)?mitglieder|"
        r"(?:mitgliedschaft|clubmitgliedschaft).{0,60}(?:voraussetzung|erforderlich|notwendig)",
        low, flags=re.I | re.S
    ))


def social_required(text: str) -> bool:
    low = norm(text)
    for platform in ("instagram", "facebook", "tiktok"):
        start = 0
        while True:
            i = low.find(platform, start)
            if i < 0:
                break
            ctx = low[max(0, i - 120): i + 220]
            if any(a in ctx for a in ("folgen", "folge", "kommentieren", "kommentar", "liken", "like", "teilen", "markieren", "taggen")) and any(
                r in ctx for r in ("teilnahme", "mitmachen", "musst", "mussen", "müssen", "voraussetzung")
            ):
                return True
            start = i + len(platform)
    return False


def call_or_sms_only(text: str, soup: BeautifulSoup) -> bool:
    low = norm(text)
    form = bool(soup.find("form")) or bool(soup.find("input", attrs={"type": re.compile("email|text", re.I)}))
    web_words = any(x in low for x in ("jetzt teilnehmen", "teilnahmeformular", "formular ausfullen", "formular ausfuellen"))
    call_ctx = bool(re.search(r"(?:teilnahme|mitmachen).{0,120}(?:anrufen|hotline|sms senden|per sms)", low, flags=re.I | re.S))
    return call_ctx and not (form or web_words)


def has_web_entry(text: str, soup: BeautifulSoup) -> bool:
    low = norm(text)
    if soup.find("form"):
        return True
    if soup.find("input", attrs={"type": re.compile("email|text", re.I)}):
        return True
    return any(x in low for x in (
        "jetzt teilnehmen", "teilnahmeformular", "am gewinnspiel teilnehmen",
        "formular ausfullen", "formular ausfuellen", "newsletter anmelden",
        "e mail senden", "per e mail",
    ))


def fetch_rules_context(base_url: str, soup: BeautifulSoup) -> str:
    """Fetch up to three same-domain rules/terms pages linked by the entry page."""
    base_domain = domain(base_url)
    links = []
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base_url, a.get("href"))
        if domain(href) != base_domain:
            continue
        marker = norm((a.get_text(" ", strip=True) or "") + " " + urllib.parse.urlsplit(href).path)
        if any(k in marker for k in (
            "teilnahmebedingungen", "gewinnspielbedingungen", "gewinnspiel bedingungen",
            "teilnahme bedingungen", "aktionsbedingungen", "teilnahmebedingungen gewinnspiel",
        )):
            links.append(canonical_url(href))
    texts = []
    for href in list(dict.fromkeys(links))[:3]:
        rr = get(href)
        if not rr:
            continue
        _ss, tt = soup_and_text(rr)
        if tt:
            texts.append(tt)
        time.sleep(0.04)
    return " ".join(texts)


def child_or_baby_specific(title: str, prize: str) -> bool:
    n = norm(title + " " + prize)
    blocked = (
        "babytrage", "baby", "kinderwagen", "windeln", "kindersitz", "kinderbuch",
        "spielzeug fur kinder", "spielzeug fuer kinder", "kids set", "kinderzimmer",
    )
    return any(x in n for x in blocked)


def infer_category(title: str, prize: str) -> str:
    n = norm(title + " " + prize)
    groups = [
        ("Reisen & Urlaub", ("reise", "hotel", "urlaub", "kreuzfahrt", "flug", "wochenende")),
        ("Technik & Elektronik", ("iphone", "ipad", "smartphone", "fernseher", "tv", "playstation", "nintendo", "kamera", "monitor", "laptop", "kopfh")),
        ("Auto & Mobilität", ("auto", "vw ", "bmw", "mercedes", "vespa", "e bike", "fahrrad", "camper", "wohnmobil")),
        ("Haus & Garten", ("garten", "grill", "werkzeug", "kuche", "küche", "matratze", "staubsauger", "mobel", "möbel")),
        ("Geld & Gutscheine", ("euro bargeld", "gutschein", "cash", "bargeld")),
        ("Freizeit & Erlebnisse", ("ticket", "konzert", "festival", "kino", "event", "theater", "museum")),
        ("Beauty & Wellness", ("duft", "parfum", "pflege", "wellness", "kosmetik")),
        ("Essen & Trinken", ("kaffee", "wein", "schokolade", "lebensmittel", "genuss", "getrank", "getränk")),
    ]
    for cat, words in groups:
        if any(w in n for w in words):
            return cat
    return "Sonstiges"


def winner_count(prize: str) -> int | None:
    vals = []
    for m in re.finditer(r"\b(\d{1,4})\s*[x×]\b", prize, flags=re.I):
        try:
            vals.append(int(m.group(1)))
        except ValueError:
            pass
    return sum(vals) if vals else None


def chance_score(winners: int | None, title: str, prize: str) -> int:
    score = 44
    if winners:
        score += min(28, 5 + int((min(winners, 200) ** 0.4) * 4))
    n = norm(title + " " + prize)
    if any(x in n for x in ("auto", "iphone", "reise", "bargeld", "playstation")):
        score -= 8
    return max(25, min(85, score))


def source_for_domain(sources: list[dict], d: str, provider: str) -> str:
    for s in sources:
        sd = str(s.get("domain", "")).lower().removeprefix("www.")
        if sd and (d == sd or d.endswith("." + sd) or sd.endswith("." + d)):
            return str(s.get("id"))
    sid = "bulk-" + re.sub(r"[^a-z0-9]+", "-", d)[:55].strip("-")
    if not any(str(s.get("id")) == sid for s in sources):
        sources.append({
            "id": sid,
            "name": provider or d,
            "domain": d,
            "country": "Deutschland",
            "countriesAllowed": ["Deutschland"],
            "type": "Bulk-Discovery",
            "categories": ["Sonstiges"],
            "automation": "yellow",
            "quality": 4,
            "active": True,
            "requiresLogin": False,
            "socialOnly": False,
            "checkIntervalDays": 3,
            "lastChecked": TODAY.isoformat(),
            "lastCatalogReview": TODAY.isoformat(),
            "notes": "Am 30.09.2026 im verifizierten Bulk-Discovery-Lauf ergänzt.",
            "germanyEligibility": "yes",
            "verification": "direct-page",
            "monitoringPriority": "medium",
            "policyReview": "bulk-discovery-strict",
        })
    return sid


def detail_metadata(detail_url: str):
    r = get(detail_url)
    if not r:
        return None
    soup, text = soup_and_text(r)
    h1 = soup.find("h1")
    title = re.sub(r"\s+", " ", h1.get_text(" ", strip=True) if h1 else "").strip()
    prize_parts = strings_after_label(
        soup, "Gewinne",
        ("Gewinnsumme", "Anzahl Gewinne", "Status", "Kategorie", "Einsendeschluss", "Veranstalter"),
        10,
    )
    prize = " · ".join(prize_parts[:8]).strip() or title

    provider_parts = strings_after_label(
        soup, "Veranstalter",
        ("Aufwand", "Lösung", "Eingetragen am", "Enddatum in", "Details", "Kurzüberblick"),
        2,
    )
    provider = provider_parts[0] if provider_parts else domain(detail_url)

    go = None
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(r.url, a.get("href"))
        if "/go/" in urllib.parse.urlsplit(href).path and "gewinnhai.de" in urllib.parse.urlsplit(href).netloc:
            label = norm(a.get_text(" ", strip=True))
            if "zum gewinnspiel" in label or not go:
                go = href
    if not go:
        return None
    return {"title": title, "prize": prize, "provider": provider, "go": go, "detailText": text}


def main():
    contests_doc = load(CONTESTS_FILE)
    sources_doc = load(SOURCES_FILE)
    version_doc = load(VERSION_FILE)

    contests = contests_doc.get("contests", [])
    sources = sources_doc.get("sources", [])
    existing_urls = {canonical_url(c.get("url", "")) for c in contests if c.get("url")}
    existing_title_keys = {norm(c.get("title", "")) for c in contests if c.get("title")}
    existing_ids = {str(c.get("id")) for c in contests}

    report = {
        "date": TODAY.isoformat(),
        "target": TARGET,
        "checkedDetails": 0,
        "officialPagesFetched": 0,
        "added": 0,
        "rejected": {},
        "items": [],
    }

    def reject(reason: str, detail_url: str, title: str = "", official_url: str = ""):
        report["rejected"][reason] = int(report["rejected"].get(reason, 0)) + 1
        if len(report.setdefault("rejectionSamples", [])) < 30:
            report["rejectionSamples"].append({
                "reason": reason, "title": title, "detail": detail_url, "official": official_url
            })

    details = collect_detail_urls()
    additions = []

    for detail_url in details:
        if len(additions) >= TARGET or time.monotonic() >= STOP_AT:
            break
        report["checkedDetails"] += 1
        meta = detail_metadata(detail_url)
        if not meta:
            reject("detail-unreadable", detail_url)
            continue

        title = meta["title"]
        prize = meta["prize"]
        if not title or child_or_baby_specific(title, prize):
            reject("child-baby-or-no-title", detail_url, title)
            continue
        if norm(title) in existing_title_keys:
            reject("duplicate-title", detail_url, title)
            continue

        go_r = get(meta["go"])
        if not go_r:
            reject("direct-link-unreachable", detail_url, title)
            continue
        official_url = canonical_url(go_r.url)
        d = domain(official_url)
        report["officialPagesFetched"] += 1

        if d in SOCIAL_HOSTS or any(d.endswith("." + x) for x in SOCIAL_HOSTS):
            reject("social-host", detail_url, title, official_url)
            continue
        if "gewinnhai.de" in d:
            reject("no-direct-organizer-link", detail_url, title, official_url)
            continue
        if official_url in existing_urls:
            reject("duplicate-url", detail_url, title, official_url)
            continue

        soup, text = soup_and_text(go_r)
        path_low = urllib.parse.urlsplit(official_url).path.lower()

        if ("teilnahmebedingungen" in path_low or "datenschutz" in path_low) and not has_web_entry(text, soup):
            reject("terms-not-entry-page", detail_url, title, official_url)
            continue

        rules_text = fetch_rules_context(official_url, soup)
        combined_text = (text + " " + rules_text).strip()

        deadline = official_deadline(combined_text)
        if not deadline:
            reject("deadline-not-confirmed", detail_url, title, official_url)
            continue
        if not has_germany_eligibility(combined_text):
            reject("germany-eligibility-not-confirmed", detail_url, title, official_url)
            continue
        if purchase_required(combined_text):
            reject("purchase-required", detail_url, title, official_url)
            continue
        if club_required(combined_text):
            reject("club-required", detail_url, title, official_url)
            continue
        if social_required(combined_text):
            reject("social-required", detail_url, title, official_url)
            continue
        if call_or_sms_only(combined_text, soup):
            reject("call-or-sms-only", detail_url, title, official_url)
            continue
        if not has_web_entry(text, soup):
            reject("web-entry-not-confirmed", detail_url, title, official_url)
            continue
        if any(x in text.lower() for x in ("gewinner stehen fest", "gewinnspiel beendet", "aktion beendet")):
            reject("marked-ended", detail_url, title, official_url)
            continue

        provider = meta["provider"]
        sid = source_for_domain(sources, d, provider)
        digest = hashlib.sha1((official_url + deadline.isoformat()).encode()).hexdigest()[:10]
        cid = f"bulk-{re.sub(r'[^a-z0-9]+', '-', d)[:28].strip('-')}-{digest}"
        if cid in existing_ids:
            reject("duplicate-id", detail_url, title, official_url)
            continue

        winners = winner_count(prize)
        score = chance_score(winners, title, prize)
        priority = "hoch" if score >= 62 else "mittel" if score >= 43 else "niedrig"
        category = infer_category(title, prize)
        entry_type = "form" if soup.find("form") else ("email" if "mailto:" in go_r.text.lower() else "web")

        item = {
            "id": cid,
            "title": title[:180],
            "provider": provider[:120],
            "prize": prize[:500],
            "url": official_url,
            "category": category,
            "country": "Deutschland",
            "deadline": deadline.strftime("%d.%m.%Y"),
            "winners": winners,
            "new": True,
            "daily": bool(re.search(r"täglich|taeglich|jeden tag", combined_text, re.I)),
            "international": False,
            "requirements": "Kostenlose Teilnahme über direkte Veranstalterseite; Deutschland-Teilnahme bestätigt",
            "purchaseRequired": False,
            "receiptRequired": False,
            "winnerKnown": bool(winners),
            "verified": TODAY.strftime("%d.%m.%Y"),
            "providerTrust": 4,
            "effort": 1 if entry_type == "form" else 2,
            "entryType": entry_type,
            "multipleEntry": bool(re.search(r"mehrfach|täglich|taeglich|jeden tag", combined_text, re.I)),
            "highValuePrize": any(x in norm(title + " " + prize) for x in ("auto", "reise", "iphone", "bargeld", "playstation", "fernseher")),
            "tags": ["Bulk Discovery", "30.09.2026", "direkter Link", "DE bestätigt"],
            "addedAt": TODAY.strftime("%d.%m.%Y"),
            "sourceId": sid,
            "deEligibility": "bestätigt",
            "participationFrequency": "täglich" if re.search(r"täglich|taeglich|jeden tag", combined_text, re.I) else "einmalig",
            "chanceScore": score,
            "priority": priority,
            "qualityScore": 97,
            "shortDescription": prize[:220],
            "dataCompleteness": 94 if winners is not None else 90,
            "lastVerified": TODAY.strftime("%d.%m.%Y"),
            "catalogStatus": "active",
            "scoutStatus": "verified-direct-page",
            "scoutAdded": False,
            "discoverySource": "GewinnHai (nur Discovery; Veranstalterseite direkt geprüft)",
        }

        additions.append(item)
        existing_urls.add(official_url)
        existing_title_keys.add(norm(title))
        existing_ids.add(cid)
        report["items"].append({
            "id": cid,
            "title": title,
            "provider": provider,
            "deadline": item["deadline"],
            "url": official_url,
        })
        time.sleep(0.10)

    contests.extend(additions)
    active_count = sum(1 for c in contests if c.get("catalogStatus") == "active")

    contests_doc["contests"] = contests
    contests_doc["version"] = "8.9.0"
    contests_doc["updated"] = TODAY.isoformat()
    contests_doc["activeCountAtRelease"] = active_count
    contests_doc["bulkDiscovery"] = {
        "lastRun": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target": TARGET,
        "added": len(additions),
        "strictPolicy": True,
        "directLinksOnly": True,
    }

    sources_doc["sources"] = sources
    sources_doc["updated"] = TODAY.isoformat()

    version_doc["catalogVersion"] = "8.9.0"
    version_doc["released"] = TODAY.isoformat()
    version_doc["catalogCount"] = len(contests)
    version_doc["activeCount"] = active_count

    report["added"] = len(additions)
    report["budgetReached"] = time.monotonic() >= STOP_AT
    report["success"] = len(additions) == TARGET

    save(CONTESTS_FILE, contests_doc)
    save(SOURCES_FILE, sources_doc)
    save(VERSION_FILE, version_doc)
    save(REPORT_FILE, report)

    print(json.dumps({
        "ok": len(additions) == TARGET,
        "target": TARGET,
        "added": len(additions),
        "checkedDetails": report["checkedDetails"],
        "officialPagesFetched": report["officialPagesFetched"],
        "activeCount": active_count,
        "rejected": report["rejected"],
    }, ensure_ascii=False, indent=2))

    if len(additions) < TARGET:
        raise SystemExit(f"Bulk discovery added only {len(additions)} of {TARGET}; review report and rerun with broader discovery.")


if __name__ == "__main__":
    main()
