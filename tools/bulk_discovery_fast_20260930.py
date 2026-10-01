#!/usr/bin/env python3
"""Parallel one-off discovery for WinWin, 2026-09-30.

Runs on a separate branch. GewinnHai is discovery-only: each candidate must
resolve to the organizer's own page and pass direct-page / linked-terms checks.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
CF = ROOT / "contests.json"
SF = ROOT / "sources.json"
VF = ROOT / "version.json"
RF = ROOT / "data" / "bulk-discovery-fast-2026-09-30.json"

TODAY = date.today()
TARGET = int(os.getenv("WINWIN_BULK_TARGET", "50"))
MIN_PUBLISH = int(os.getenv("WINWIN_BULK_MIN_PUBLISH", "30"))
WORKERS = int(os.getenv("WINWIN_BULK_WORKERS", "16"))
MAX_DETAILS = int(os.getenv("WINWIN_BULK_MAX_DETAILS", "900"))
TIMEOUT = 12

UA = "WinWin-Fast-Bulk/8.9.1 (+https://github.com/yztg676k7r-arch/WinWin)"
SOCIAL = ("instagram.com", "facebook.com", "tiktok.com", "x.com", "twitter.com")
MONTHS = {
    "januar":1,"februar":2,"märz":3,"maerz":3,"april":4,"mai":5,"juni":6,
    "juli":7,"august":8,"september":9,"oktober":10,"november":11,"dezember":12,
}

INDEX_PAGES = [
    *(f"https://www.gewinnhai.de/gewinnspiele/oktober-2026?page={i}" for i in range(1, 12)),
    *(f"https://www.gewinnhai.de/gewinnspiele/november-2026?page={i}" for i in range(1, 6)),
    *(f"https://www.gewinnhai.de/gewinnspiele/dezember-2026?page={i}" for i in range(1, 4)),
    *(f"https://www.gewinnhai.de/gewinnspiele/januar-2027?page={i}" for i in range(1, 3)),
    "https://www.gewinnhai.de/neu",
    *(f"https://www.gewinnhai.de/gewinnspiele?page={i}" for i in range(1, 26)),
]

DATE_TOKEN = r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\.?\s+(?:Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+\d{4})"
DATE_PATTERNS = [
    rf"(?:teilnahmeschluss|einsendeschluss|aktionsende|teilnahmefrist|gewinnspielende)\s*[:\-]?\s*(?:am\s*)?{DATE_TOKEN}",
    rf"(?:gewinnspiel|aktion|teilnahme|aktionszeitraum|teilnahmezeitraum|gewinnspielzeitraum).{{0,260}}?(?:endet\s*(?:am)?|läuft\s*(?:bis|bis zum)?|laeuft\s*(?:bis|bis zum)?|bis\s*(?:zum|einschließlich|einschliesslich)?|ende\s*[:\-]?)\s*(?:am\s*)?{DATE_TOKEN}",
    rf"(?:teilnahme|mitmachen).{{0,120}}?(?:möglich|moeglich)?\s*(?:bis|bis zum)\s*(?:einschließlich|einschliesslich)?\s*{DATE_TOKEN}",
]

def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))

def save(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii","ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+"," ",s).strip()

def canon(u: str) -> str:
    p=urllib.parse.urlsplit(u)
    q=[(k,v) for k,v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith("utm_") and k.lower() not in {"fbclid","gclid"}]
    return urllib.parse.urlunsplit((p.scheme.lower() or "https",p.netloc.lower(),re.sub(r"/+","/",p.path or "/").rstrip("/") or "/",urllib.parse.urlencode(q),""))

def dom(u: str) -> str:
    return urllib.parse.urlsplit(u).netloc.lower().removeprefix("www.")

def related_domain(a: str, b: str) -> bool:
    a=dom(a); b=dom(b)
    return a==b or a.endswith("." + b) or b.endswith("." + a)

def session():
    s=requests.Session()
    s.headers.update({"User-Agent":UA,"Accept-Language":"de-DE,de;q=0.9,en;q=0.4"})
    return s

def get_html(s: requests.Session, u: str):
    try:
        r=s.get(u,timeout=TIMEOUT,allow_redirects=True)
        if r.status_code>=400 or len(r.content)>5_000_000: return None
        if "html" not in r.headers.get("content-type","html") and "text" not in r.headers.get("content-type","html"): return None
        soup=BeautifulSoup(r.text,"html.parser")
        for t in soup(["script","style","noscript","svg"]): t.decompose()
        return r, soup, " ".join(soup.stripped_strings)[:350_000]
    except requests.RequestException:
        return None

def parse_date(raw: str):
    raw0=re.sub(r"\s+"," ",raw.strip().lower())
    m=re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})",raw0)
    if m:
        try:return date(int(m.group(1)),int(m.group(2)),int(m.group(3)))
        except ValueError:return None
    raw=raw0.replace("/",".").replace("-",".")
    m=re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})",raw)
    if m:
        y=int(m.group(3)); y += 2000 if y<100 else 0
        try:return date(y,int(m.group(2)),int(m.group(1)))
        except ValueError:return None
    m=re.fullmatch(r"(\d{1,2})\.?\s+([a-zä]+)\s+(\d{4})",raw)
    if m and m.group(2) in MONTHS:
        try:return date(int(m.group(3)),MONTHS[m.group(2)],int(m.group(1)))
        except ValueError:return None
    return None

def deadline(text: str):
    ds=[]
    for pat in DATE_PATTERNS:
        for m in re.finditer(pat,text,re.I|re.S):
            d=parse_date(m.group(1))
            if d and TODAY+timedelta(days=1)<=d<=TODAY+timedelta(days=370): ds.append(d)
    return min(ds) if ds else None

def germany_ok(text: str) -> bool:
    low=text.lower()
    pats=[
        r"(?:wohnsitz|wohnhaft|wohnort).{0,180}(?:deutschland|bundesrepublik)",
        r"(?:teilnahmeberechtigt|teilnehmen können|teilnehmen duerfen|teilnehmen dürfen).{0,240}(?:deutschland|bundesrepublik)",
        r"(?:deutschland|bundesrepublik).{0,180}(?:wohnsitz|wohnhaft|teilnahmeberechtigt|teilnehmen)",
        r"(?:deutschland,\s*österreich|deutschland,\s*oesterreich|deutschland und österreich|deutschland und oesterreich).{0,120}(?:teilnahme|wohnsitz|wohnhaft)",
    ]
    return any(re.search(p,low,re.I|re.S) for p in pats)

def purchase_bad(text: str) -> bool:
    low=text.lower()
    low=re.sub(r"(?:kein(?:e|en|er)?|ohne)\s+(?:produkt)?kauf.{0,45}(?:notwendig|erforderlich|voraussetzung|pflicht)","",low)
    pats=[
        r"kassenbon.{0,35}(?:hochladen|einsenden|fotografieren)",
        r"kaufbeleg.{0,35}(?:hochladen|einsenden|fotografieren)",
        r"\bbon\b.{0,35}(?:hochladen|einsenden|fotografieren)",
        r"rechnung.{0,35}(?:hochladen|einsenden)",
        r"mindestbestellwert",
        r"aktionsprodukt(?:e)?.{0,35}(?:kaufen|erwerben)",
        r"(?:teilnahme|gewinnspiel).{0,100}\bkauf\b.{0,50}(?:voraussetzung|erforderlich|notwendig|pflicht)",
        r"\bkauf\b.{0,50}(?:voraussetzung|erforderlich|notwendig|pflicht).{0,100}(?:teilnahme|gewinnspiel)",
    ]
    return any(re.search(p,low,re.I|re.S) for p in pats)

def club_bad(text: str) -> bool:
    return bool(re.search(r"(?:nur|ausschließlich|ausschliesslich).{0,50}(?:club)?mitglieder|(?:mitgliedschaft|clubmitgliedschaft).{0,70}(?:voraussetzung|erforderlich|notwendig)",text.lower(),re.I|re.S))

def social_bad(text: str) -> bool:
    n=norm(text)
    for platform in ("instagram","facebook","tiktok"):
        for m in re.finditer(platform,n):
            ctx=n[max(0,m.start()-180):m.end()+260]
            if any(x in ctx for x in ("folgen","folge","kommentieren","kommentar","liken","like","teilen","markieren","taggen")) and any(x in ctx for x in ("teilnahme","mitmachen","musst","mussen","voraussetzung","um teilzunehmen")):
                return True
    return False

def paid_bad(text: str) -> bool:
    low=text.lower()
    return bool(re.search(r"premium[- ]?sms|0137\d|0900\d|kostenpflichtig.{0,30}(?:anruf|sms|teilnahme|abo)|spieleinsatz|loseinsatz",low,re.I|re.S))

def has_entry(text: str, soup: BeautifulSoup) -> bool:
    n=norm(text)
    if soup.find("form"): return True
    if soup.find("input",attrs={"type":re.compile("email|text",re.I)}): return True
    return any(x in n for x in ("jetzt teilnehmen","teilnahmeformular","am gewinnspiel teilnehmen","formular ausfullen","formular ausfuellen","per e mail","e mail senden","newsletter anmelden"))

def call_only(text: str, soup: BeautifulSoup) -> bool:
    n=norm(text)
    call=bool(re.search(r"(?:teilnahme|mitmachen).{0,140}(?:anrufen|hotline|sms senden|per sms)",n,re.I|re.S))
    return call and not has_entry(text,soup)

def child_baby(title: str, prize: str) -> bool:
    n=norm(title+" "+prize)
    return any(x in n for x in ("babytrage","baby","kinderwagen","windeln","kindersitz","kinderzimmer","spielzeug fur kinder","spielzeug fuer kinder"))

def strings_after(soup: BeautifulSoup,label: str,stops: tuple[str,...],limit=8):
    vals=list(soup.stripped_strings); nl=norm(label)
    for i,x in enumerate(vals):
        if norm(x)==nl:
            out=[]
            for y in vals[i+1:]:
                if any(norm(y)==norm(z) for z in stops): break
                if y and y not in out: out.append(y)
                if len(out)>=limit: break
            return out
    return []

def pdf_text(s: requests.Session,u: str) -> str:
    try:
        r=s.get(u,timeout=TIMEOUT,allow_redirects=True)
        if r.status_code>=400 or len(r.content)>6_000_000:return ""
        if "pdf" not in r.headers.get("content-type","").lower() and not urllib.parse.urlsplit(r.url).path.lower().endswith(".pdf"):return ""
        reader=PdfReader(io.BytesIO(r.content))
        return " ".join((p.extract_text() or "") for p in reader.pages[:20])[:250_000]
    except Exception:
        return ""

def terms_text(s: requests.Session,base_url: str,soup: BeautifulSoup) -> str:
    links=[]
    for a in soup.find_all("a",href=True):
        href=urllib.parse.urljoin(base_url,a.get("href"))
        marker=norm((a.get_text(" ",strip=True) or "")+" "+urllib.parse.urlsplit(href).path)
        if not any(k in marker for k in ("teilnahmebedingungen","gewinnspielbedingungen","teilnahme bedingungen","aktionsbedingungen","gewinnspiel bedingungen","bedingungen gewinnspiel")):
            continue
        if related_domain(base_url,href) or "teilnahme" in marker or "gewinnspiel" in marker:
            links.append(canon(href))
    out=[]
    for u in list(dict.fromkeys(links))[:4]:
        if urllib.parse.urlsplit(u).path.lower().endswith(".pdf"):
            t=pdf_text(s,u)
        else:
            got=get_html(s,u); t=got[2] if got else ""
        if t:out.append(t)
    return " ".join(out)

def detail_info(s: requests.Session,u: str):
    got=get_html(s,u)
    if not got:return None
    r,soup,text=got
    h=soup.find("h1")
    title=re.sub(r"\s+"," ",h.get_text(" ",strip=True) if h else "").strip()
    prize=" · ".join(strings_after(soup,"Gewinne",("Gewinnsumme","Anzahl Gewinne","Status","Kategorie","Einsendeschluss","Veranstalter"),10)) or title
    provs=strings_after(soup,"Veranstalter",("Aufwand","Lösung","Eingetragen am","Enddatum in","Details","Kurzüberblick"),2)
    provider=provs[0] if provs else dom(u)
    go=None
    for a in soup.find_all("a",href=True):
        href=urllib.parse.urljoin(r.url,a.get("href"))
        if "gewinnhai.de" in urllib.parse.urlsplit(href).netloc and "/go/" in urllib.parse.urlsplit(href).path:
            if "zum gewinnspiel" in norm(a.get_text(" ",strip=True)) or not go:go=href
    return (title,prize,provider,go)

def inspect(detail_url: str):
    s=session()
    meta=detail_info(s,detail_url)
    if not meta:return {"ok":False,"reason":"detail-unreadable","detail":detail_url}
    title,prize,provider,go=meta
    if not title or child_baby(title,prize):return {"ok":False,"reason":"child-baby-or-title","detail":detail_url,"title":title}
    if not go:return {"ok":False,"reason":"no-go-link","detail":detail_url,"title":title}
    got=get_html(s,go)
    if not got:return {"ok":False,"reason":"direct-unreachable","detail":detail_url,"title":title}
    r,soup,text=got
    url=canon(r.url); d=dom(url)
    if "gewinnhai.de" in d or any(d==x or d.endswith("."+x) for x in SOCIAL):
        return {"ok":False,"reason":"non-organizer-or-social-host","detail":detail_url,"title":title,"url":url}
    path=urllib.parse.urlsplit(url).path.lower()
    if ("teilnahmebedingungen" in path or "datenschutz" in path) and not has_entry(text,soup):
        return {"ok":False,"reason":"terms-only","detail":detail_url,"title":title,"url":url}
    rules=terms_text(s,url,soup)
    combined=(text+" "+rules).strip()
    dline=deadline(combined)
    if not dline:return {"ok":False,"reason":"deadline","detail":detail_url,"title":title,"url":url}
    if not germany_ok(combined):return {"ok":False,"reason":"germany","detail":detail_url,"title":title,"url":url}
    if purchase_bad(combined):return {"ok":False,"reason":"purchase","detail":detail_url,"title":title,"url":url}
    if club_bad(combined):return {"ok":False,"reason":"club","detail":detail_url,"title":title,"url":url}
    if social_bad(combined):return {"ok":False,"reason":"social-required","detail":detail_url,"title":title,"url":url}
    if paid_bad(combined):return {"ok":False,"reason":"paid","detail":detail_url,"title":title,"url":url}
    if call_only(combined,soup):return {"ok":False,"reason":"call-sms-only","detail":detail_url,"title":title,"url":url}
    if not has_entry(text,soup):return {"ok":False,"reason":"entry","detail":detail_url,"title":title,"url":url}
    if any(x in text.lower() for x in ("gewinner stehen fest","gewinnspiel beendet","aktion beendet","verlosung beendet")):
        return {"ok":False,"reason":"ended","detail":detail_url,"title":title,"url":url}
    return {"ok":True,"title":title,"prize":prize,"provider":provider,"url":url,"domain":d,"deadline":dline,"text":combined[:50000],"hasForm":bool(soup.find("form")),"hasMailto":"mailto:" in r.text.lower()}

def collect_details():
    s=session(); found=[]
    for page in INDEX_PAGES:
        got=get_html(s,page)
        if not got:continue
        r,soup,_=got
        for a in soup.find_all("a",href=True):
            u=urllib.parse.urljoin(r.url,a.get("href"))
            p=urllib.parse.urlsplit(u)
            if p.netloc.endswith("gewinnhai.de") and p.path.startswith("/gewinnspiel/") and p.path.count("/")>=2:
                found.append(canon(u))
        if len(set(found))>=MAX_DETAILS:break
    return list(dict.fromkeys(found))[:MAX_DETAILS]

def category(title,prize):
    n=norm(title+" "+prize)
    groups=[
        ("Reisen & Urlaub",("reise","hotel","urlaub","kreuzfahrt","flug","wochenende")),
        ("Technik & Elektronik",("iphone","ipad","smartphone","fernseher","playstation","nintendo","kamera","monitor","laptop","kopfhorer")),
        ("Auto & Mobilität",("auto","bmw","mercedes","vespa","e bike","fahrrad","camper","wohnmobil")),
        ("Haus & Garten",("garten","grill","werkzeug","kuche","matratze","staubsauger","mobel")),
        ("Geld & Gutscheine",("bargeld","gutschein","cash")),
        ("Freizeit & Erlebnisse",("ticket","konzert","festival","kino","event","theater","museum")),
        ("Beauty & Wellness",("duft","parfum","pflege","wellness","kosmetik")),
        ("Essen & Trinken",("kaffee","wein","schokolade","lebensmittel","genuss","getrank")),
    ]
    for c,words in groups:
        if any(w in n for w in words):return c
    return "Sonstiges"

def winners(prize):
    vals=[int(x) for x in re.findall(r"\b(\d{1,4})\s*[x×]\b",prize)]
    return sum(vals) if vals else None

def score(win,title,prize):
    v=44+(min(28,5+int((min(win,200)**0.4)*4)) if win else 0)
    if any(x in norm(title+" "+prize) for x in ("auto","iphone","reise","bargeld","playstation")):v-=8
    return max(25,min(85,v))

def source_id(sources,d,provider):
    for src in sources:
        sd=str(src.get("domain","")).lower().removeprefix("www.")
        if sd and (d==sd or d.endswith("."+sd) or sd.endswith("."+d)):return str(src.get("id"))
    sid="bulk-"+re.sub(r"[^a-z0-9]+","-",d)[:55].strip("-")
    if not any(str(x.get("id"))==sid for x in sources):
        sources.append({
            "id":sid,"name":provider or d,"domain":d,"country":"Deutschland","countriesAllowed":["Deutschland"],
            "type":"Bulk-Discovery","categories":["Sonstiges"],"automation":"yellow","quality":4,"active":True,
            "requiresLogin":False,"socialOnly":False,"checkIntervalDays":3,"lastChecked":TODAY.isoformat(),
            "lastCatalogReview":TODAY.isoformat(),"notes":f"Am {TODAY.strftime('%d.%m.%Y')} per parallelem Bulk-Discovery-Lauf direkt geprüft.",
            "germanyEligibility":"yes","verification":"direct-page+terms","monitoringPriority":"medium","policyReview":"bulk-discovery-strict",
        })
    return sid

def main():
    cd=load(CF); sd=load(SF); vd=load(VF)
    contests=cd.get("contests",[]); sources=sd.get("sources",[])
    existing_urls={canon(x.get("url","")) for x in contests if x.get("url")}
    existing_titles={norm(x.get("title","")) for x in contests if x.get("title")}
    existing_ids={str(x.get("id")) for x in contests}

    details=collect_details()
    results=[]; reject={}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs={ex.submit(inspect,u):u for u in details}
        for fut in as_completed(futs):
            try:r=fut.result()
            except Exception as e:r={"ok":False,"reason":"worker-error","detail":futs[fut],"error":str(e)[:160]}
            if r.get("ok"):results.append(r)
            else:reject[r.get("reason","unknown")]=reject.get(r.get("reason","unknown"),0)+1

    # deterministic: earliest deadline first, then title
    results.sort(key=lambda x:(x["deadline"],norm(x["title"])))
    additions=[]; seen_urls=set(); seen_titles=set()
    for r in results:
        if len(additions)>=TARGET:break
        u=canon(r["url"]); tk=norm(r["title"])
        if u in existing_urls or u in seen_urls:continue
        if tk in existing_titles or tk in seen_titles:continue
        w=winners(r["prize"]); sc=score(w,r["title"],r["prize"])
        sid=source_id(sources,r["domain"],r["provider"])
        digest=hashlib.sha1((u+r["deadline"].isoformat()).encode()).hexdigest()[:10]
        cid=f"bulk-{re.sub(r'[^a-z0-9]+','-',r['domain'])[:28].strip('-')}-{digest}"
        if cid in existing_ids:continue
        entry="form" if r["hasForm"] else ("email" if r["hasMailto"] else "web")
        txt=r["text"]
        item={
            "id":cid,"title":r["title"][:180],"provider":r["provider"][:120],"prize":r["prize"][:500],
            "url":u,"category":category(r["title"],r["prize"]),"country":"Deutschland",
            "deadline":r["deadline"].strftime("%d.%m.%Y"),"winners":w,"new":True,
            "daily":bool(re.search(r"täglich|taeglich|jeden tag",txt,re.I)),"international":False,
            "requirements":"Kostenlose Teilnahme über direkte Veranstalterseite; Deutschland-Teilnahme bestätigt",
            "purchaseRequired":False,"receiptRequired":False,"winnerKnown":bool(w),
            "verified":TODAY.strftime("%d.%m.%Y"),"providerTrust":4,"effort":1 if entry=="form" else 2,
            "entryType":entry,"multipleEntry":bool(re.search(r"mehrfach|täglich|taeglich|jeden tag",txt,re.I)),
            "highValuePrize":any(x in norm(r["title"]+" "+r["prize"]) for x in ("auto","reise","iphone","bargeld","playstation","fernseher")),
            "tags":["Bulk Discovery","30.09.2026","direkter Link","DE bestätigt"],"addedAt":TODAY.strftime("%d.%m.%Y"),
            "sourceId":sid,"deEligibility":"bestätigt","participationFrequency":"täglich" if re.search(r"täglich|taeglich|jeden tag",txt,re.I) else "einmalig",
            "chanceScore":sc,"priority":"hoch" if sc>=62 else "mittel" if sc>=43 else "niedrig",
            "qualityScore":98,"shortDescription":r["prize"][:220],"dataCompleteness":94 if w is not None else 90,
            "lastVerified":TODAY.strftime("%d.%m.%Y"),"catalogStatus":"active","scoutStatus":"verified-direct-page+terms",
            "scoutAdded":False,"discoverySource":"GewinnHai (nur Discovery; Veranstalterseite/Teilnahmebedingungen direkt geprüft)",
        }
        additions.append(item); seen_urls.add(u); seen_titles.add(tk); existing_ids.add(cid)

    contests.extend(additions)
    active=sum(1 for x in contests if x.get("catalogStatus")=="active")
    cd["contests"]=contests; cd["version"]=str(vd.get("version") or cd.get("version") or "8.9.1"); cd["updated"]=TODAY.isoformat(); cd["activeCountAtRelease"]=active
    cd["bulkDiscovery"]={"lastRun":datetime.now().astimezone().isoformat(timespec="seconds"),"target":TARGET,"added":len(additions),"strictPolicy":True,"directLinksOnly":True,"parallel":True}
    sd["sources"]=sources; sd["updated"]=TODAY.isoformat()
    vd["catalogVersion"]=str(vd.get("version") or "8.9.1"); vd["released"]=TODAY.isoformat(); vd["catalogCount"]=len(contests); vd["activeCount"]=active
    report={
        "date":TODAY.isoformat(),"target":TARGET,"detailCandidates":len(details),"verifiedCandidates":len(results),
        "added":len(additions),"success":len(additions)>=MIN_PUBLISH,"minPublish":MIN_PUBLISH,"activeCount":active,"rejected":reject,
        "items":[{"id":x["id"],"title":x["title"],"provider":x["provider"],"deadline":x["deadline"],"url":x["url"]} for x in additions],
    }
    save(CF,cd); save(SF,sd); save(VF,vd); save(RF,report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if len(additions)<MIN_PUBLISH:raise SystemExit(f"Only {len(additions)} verified; minimum publish threshold is {MIN_PUBLISH}")

if __name__=="__main__":
    main()
