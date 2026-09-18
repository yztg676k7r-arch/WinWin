import importlib.util,json,tempfile,time,yaml
from pathlib import Path
from datetime import timedelta
from bs4 import BeautifulSoup
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('review',root/'scripts/review_queue.py');r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
end=(r.TODAY+timedelta(days=10)).strftime('%d.%m.%Y')
text=f'Wir verlosen einen Reisegutschein. Die Teilnahme ist kostenlos. Teilnahmeberechtigt sind Personen mit Wohnsitz in Deutschland. Teilnahmeschluss: {end}.'
raw='<h1>Reisegutschein gewinnen</h1><form><input type="email"></form><p>'+text+'</p>'
soup=BeautifulSoup(raw,'html.parser')
assert r.extract_prize(text,'Reisegutschein gewinnen')=='Wir verlosen einen Reisegutschein.'
assert r.classify('https://example.de/gewinnspiel','Reisegutschein gewinnen',text,soup,raw)[0]=='verified'
for bad in ['Ein Kassenbon ist erforderlich.','Nur für Clubmitglieder.','Folgen auf Instagram erforderlich.']:
 assert r.classify('https://example.de/gewinnspiel','Reisegutschein gewinnen',text+bad,soup,raw)[0]!='verified'
assert r.classify('https://example.de/gewinnspiel','Reisegutschein gewinnen',text.replace('Deutschland','Österreich'),soup,raw)[0]!='verified'
# A budget stop must retain every unprocessed candidate and leave existing contests intact.
with tempfile.TemporaryDirectory() as folder:
 r.CF=Path(folder)/'contests.json';r.SF=Path(folder)/'sources.json';r.RF=Path(folder)/'review.json';r.PF=Path(folder)/'report.json'
 contests=[{'id':'existing','catalogStatus':'active','note':'preserve'}]
 pending=[{'url':'https://example.de/'+str(i),'status':'review'} for i in range(240)]
 r.save(r.CF,{'contests':contests});r.save(r.SF,{'sources':[]});r.save(r.RF,{'items':pending});r.STOP_AT=time.monotonic()-1;r.main()
 assert r.load(r.RF)['items']==pending
 assert r.load(r.CF)['contests']==contests
w=yaml.safe_load((root/'.github/workflows/daily-scout.yml').read_text())
steps=w['jobs']['scout']['steps'];assert not any('gate' in str(s) for s in steps)
print('PASS: delayed runs have no clock gate; prize extraction; policy checks; budget preserves queue and catalog')
