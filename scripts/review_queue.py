#!/usr/bin/env python3
"""Second-pass quality gate for Win Win Daily Scout.

Re-checks the Daily Scout review queue with stricter contest-specific signals,
promotes only high-confidence official web contests, rejects obvious non-contest
pages, and keeps genuinely ambiguous pages for another future pass.
"""
from __future__ import annotations
import hashlib, json, re, urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]
CF=ROOT/'contests.json'; SF=ROOT/'sources.json'; RF=ROOT/'data/daily-scout-review.json'; PF=ROOT/'data/daily-scout-report.json'
TODAY=date.today(); SESSION=requests.Session(); SESSION.headers.update({'User-Agent':'WinWin-Review-Gate/8.4.1','Accept-Language':'de-DE,de;q=0.9'})
MAX_REVIEW=220; TIMEOUT=12
CONTEST_WORDS=('gewinnspiel','verlosung','gewinnen','giveaway')
NON_CONTEST=('gewinner stehen fest','die gewinner','eventkalender','ausbildung','seminar','kurs','produkttest','winterreifen-test','gebrauchtwagen','styletrend','firmenwagen award')
BAD=(r'kassenbon',r'kaufbeleg',r'bon hochladen',r'produkt(?:e)? kaufen',r'mindestbestellwert',r'premium[- ]?sms',r'0137\d',r'0900\d',r'kostenpflichtig.{0,25}(?:anruf|sms|teilnahme|abo)',r'nur f[uü]r (?:club)?mitglieder',r'kostenpflichtige mitgliedschaft')
DATE_PATTERNS=(r'(?:teilnahmeschluss|einsendeschluss|aktionsende|endet am|bis zum)\s*[:\-]?\s*(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})',)

def load(p): return json.loads(p.read_text(encoding='utf-8'))
def save(p,x): p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def norm(s): return re.sub(r'[^a-z0-9]+',' ',str(s or '').lower().replace('ä','ae').replace('ö','oe').replace('ü','ue').replace('ß','ss')).strip()
def canon(u):
 p=urllib.parse.urlsplit(u); q=[(k,v) for k,v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith('utm_')]; return urllib.parse.urlunsplit((p.scheme,p.netloc.lower(),p.path.rstrip('/') or '/',urllib.parse.urlencode(q),''))
def fetch(u):
 try:
  r=SESSION.get(u,timeout=TIMEOUT,allow_redirects=True)
  if r.status_code>=400 or 'html' not in r.headers.get('content-type','html'): return None
  soup=BeautifulSoup(r.text,'html.parser'); [t.decompose() for t in soup(['script','style','noscript','svg'])]
  return canon(r.url),soup,' '.join(soup.stripped_strings)[:180000],r.text[:400000]
 except requests.RequestException:return None
def deadline(text):
 for pat in DATE_PATTERNS:
  for m in re.finditer(pat,text,re.I):
   raw=m.group(1).replace('/','.').replace('-','.')
   try:
    d=datetime.strptime(raw,'%d.%m.%Y').date()
    if TODAY<=d<=TODAY+timedelta(days=370): return d
   except ValueError:
    try:
     d=datetime.strptime(raw,'%d.%m.%y').date()
     if TODAY<=d<=TODAY+timedelta(days=370): return d
    except ValueError: pass
 return None
def title(soup):
 h=soup.find('h1'); x=h.get_text(' ',strip=True) if h else (soup.title.get_text(' ',strip=True) if soup.title else ''); return re.sub(r'\s+',' ',x)[:160]
def has_entry(soup,raw):
 low=raw.lower(); return bool(soup.find('form')) or any(x in low for x in ('teilnahmeformular','jetzt teilnehmen','am gewinnspiel teilnehmen','type="email"',"type='email'",'mailto:'))
def extract_prize(text,title):
 for chunk in re.split(r'(?<=[.!?])\\s+|\\n+',text):
  clean=re.sub(r'\\s+',' ',chunk).strip()
  if 12<=len(clean)<=260 and re.search(r'\\b(?:verlosen|verlost|gewinnen|gewinnt|zu gewinnen)\\b',clean,re.I):
   return clean[:220]
 return title[:220] if re.search(r'\\b(?:gewinnen|gewinn)\\b',title,re.I) else None

def classify(url,t,text,soup,raw):
 n=norm(t+' '+url); low=text.lower()
 if any(x in n for x in NON_CONTEST): return 'rejected','non-contest-page',None
 # Reject collection pages that mix several unrelated contests. A newsletter
 # form or a date elsewhere on such a hub must never qualify as an entry route.
 contest_links=set()
 for a in soup.find_all('a',href=True):
  href=urllib.parse.urljoin(url,a.get('href',''))
  if 'gewinnspiel' in urllib.parse.urlsplit(href).path.lower():
   contest_links.add(canon(href))
 if 'aktuelle gewinnspiele' in low and len(contest_links)>=2:
  return 'rejected','contest-hub',None
 path=urllib.parse.urlsplit(url).path.rstrip('/')
 if path in ('','/'): return 'rejected','generic-homepage',None
 if any(re.search(p,low,re.I) for p in BAD): return 'rejected','policy-exclusion',None
 # Require contest intent in URL/title, not merely somewhere in a generic page.
 if not any(w in n for w in CONTEST_WORDS): return 'review','contest-intent-not-specific',None
 if not extract_prize(text,t): return 'review','prize-not-unambiguous',None
 d=deadline(text)
 if not d: return 'review','deadline-not-unambiguous',None
 if not has_entry(soup,raw): return 'review','entry-route-not-unambiguous',None
 social=any(x in norm(text) for x in ('instagram','facebook','tiktok')) and any(x in norm(text) for x in ('folgen','kommentieren','liken','markieren','teilen'))
 if social and not soup.find('form') and 'mailto:' not in raw.lower(): return 'rejected','social-only',None
 return 'verified','second-pass-verified',d
def main():
 cd=load(CF); sd=load(SF); rd=load(RF) if RF.exists() else {'items':[]}; contests=cd['contests']; sources=sd['sources']; sm={s.get('id'):s for s in sources}; urls={canon(c.get('url','')) for c in contests if c.get('url')}; ids={str(c.get('id')) for c in contests}
 promoted=[]; kept=[]; rejected=[]; checked=0
 for old in rd.get('items',[])[:MAX_REVIEW]:
  if old.get('status')=='rejected': rejected.append(old); continue
  got=fetch(old.get('url','')); checked+=1
  if not got: old['reviewAttempts']=int(old.get('reviewAttempts',0))+1; old['reason']='fetch-failed'; kept.append(old); continue
  u,soup,text,raw=got; t=title(soup); status,reason,d=classify(u,t,text,soup,raw)
  if status=='rejected': old.update({'url':u,'title':t,'status':'rejected','reason':reason,'lastReviewed':TODAY.isoformat()}); rejected.append(old); continue
  if status=='review': old.update({'url':u,'title':t,'status':'review','reason':reason,'lastReviewed':TODAY.isoformat(),'reviewAttempts':int(old.get('reviewAttempts',0))+1}); kept.append(old); continue
  if canon(u) in urls: continue
  src=sm.get(old.get('sourceId'),{}); sid=src.get('id') or old.get('sourceId') or 'scout'; digest=hashlib.sha1((u+d.isoformat()).encode()).hexdigest()[:8]; cid=f'{sid}-{d.strftime("%Y%m%d")}-{digest}'
  if cid in ids: continue
  item={'id':cid,'title':t,'provider':src.get('name') or urllib.parse.urlsplit(u).netloc,'prize':extract_prize(text,t) or t,'url':u,'category':(src.get('categories') or ['Sonstiges'])[0],'country':'Deutschland','deadline':d.strftime('%d.%m.%Y'),'winners':None,'new':True,'daily':bool(re.search(r'täglich|taeglich|jeden tag',text,re.I)),'international':False,'requirements':'Kostenlose Teilnahme über offizielles Web-Angebot; automatisch zweifach geprüft','purchaseRequired':False,'receiptRequired':False,'winnerKnown':False,'verified':TODAY.strftime('%d.%m.%Y'),'providerTrust':min(5,max(3,int(src.get('quality') or 4))),'effort':1,'entryType':'form' if soup.find('form') else 'email','multipleEntry':bool(re.search(r'täglich|taeglich|jeden tag|mehrfach',text,re.I)),'highValuePrize':False,'tags':['Daily Scout','2× geprüft','kostenlos','neu'],'addedAt':TODAY.strftime('%d.%m.%Y'),'sourceId':sid,'deEligibility':'bestätigt','participationFrequency':'täglich' if re.search(r'täglich|taeglich|jeden tag',text,re.I) else 'einmalig','chanceScore':45,'priority':'mittel','qualityScore':97,'shortDescription':t,'dataCompleteness':88,'lastVerified':TODAY.strftime('%d.%m.%Y'),'catalogStatus':'active','scoutStatus':'verified-second-pass','scoutAdded':True}
  contests.append(item); promoted.append(cid); ids.add(cid); urls.add(canon(u))
 # Audit auto-published pages from the first pass: obvious non-contest pages are hidden, never deleted.
 archived=[]
 for c in contests:
  if not c.get('scoutAdded') or c.get('catalogStatus')!='active': continue
  n=norm(c.get('title','')+' '+c.get('url',''))
  if any(x in n for x in NON_CONTEST) or not any(w in n for w in CONTEST_WORDS):
   c['catalogStatus']='review'; c['scoutStatus']='needs-second-pass'; archived.append(c.get('id'))
 cd['contests']=contests; cd['activeCountAtRelease']=sum(1 for c in contests if c.get('catalogStatus')=='active'); cd['secondPass']={'lastRun':datetime.now().astimezone().isoformat(timespec='seconds'),'checked':checked,'promoted':len(promoted),'keptForReview':len(kept),'rejected':len(rejected),'autoHiddenForReview':len(archived)}
 save(CF,cd); save(RF,{'date':TODAY.isoformat(),'items':kept+rejected[-80:],'secondPass':cd['secondPass']})
 report=load(PF) if PF.exists() else {'date':TODAY.isoformat()}; report['secondPass']=cd['secondPass']; report['secondPass']['promotedIds']=promoted; report['secondPass']['autoHiddenIds']=archived; save(PF,report)
 print(json.dumps({'ok':True,**cd['secondPass']},ensure_ascii=False))
if __name__=='__main__': main()
