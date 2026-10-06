// Run with: NODE_PATH=/path/to/jsdom/node_modules node tests/full-app.cjs
const {JSDOM}=require('jsdom'),fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
const html=fs.readFileSync('index.html','utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'');
const dom=new JSDOM(html,{url:'https://winwin.test/',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window;const errors=[];w.addEventListener('error',e=>errors.push(e.message));w.scrollTo=()=>{};w.confirm=()=>true;w.requestIdleCallback=fn=>setTimeout(fn,0);
w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
const original=JSON.parse(fs.readFileSync('contests.json'));const base=original.contests.find(x=>x.id==='funke-madegood-herbst-202610');assert(base);
const fixture={...base,id:'test-new-duplicate',provider:'FUNKE FUN',url:'https://funke.fun/gewinnspiele/tvdirekt/detail-353'};
const data={...original,contests:[...original.contests,fixture]};
w.fetch=async input=>{const u=new URL(input,w.location.href);let p=u.pathname.split('/').pop();const payload=p==='contests.json'?data:JSON.parse(fs.readFileSync(p));return {ok:true,text:async()=>JSON.stringify(payload),json:async()=>payload}};
w.localStorage.setItem('gewinnen-user-v1',JSON.stringify({items:{[base.id]:{done:true,doneAt:'2026-10-05T12:00:00Z',favorite:true,note:'Must remain'},'test-new-duplicate':{done:false}},clicks:{},urlIndex:{}}));
w.eval(fs.readFileSync('contest-history.js','utf8')+'\n'+fs.readFileSync('app.js','utf8')+'\nwindow.__testEval=(expression)=>eval(expression);');
await new Promise(resolve=>setTimeout(resolve,100));
assert(!errors.length,errors.join('\n'));
assert(w.__testEval('!isOpenContest(contests.find(x=>x.id==="test-new-duplicate"))'),'duplicate filtered');
assert.equal(w.__testEval('user.items["test-new-duplicate"].done'),true);
assert.equal(w.__testEval('user.items["test-new-duplicate"].note'),'Must remain');
let count=w.document.querySelectorAll('#contestList article').length;assert.equal(count,30);
w.document.querySelector('#loadMoreContests').click();assert.equal(w.document.querySelectorAll('#contestList article').length,60);
w.document.querySelector('[data-view="moreView"]').click();assert(w.document.querySelector('#moreView').classList.contains('active'));
w.document.querySelector('[data-view="discoverView"]').click();assert(w.document.querySelector('#discoverView').classList.contains('active'));
const open=w.__testEval('discoverItems()[0].id');w.toggleDone(open);assert(!w.__testEval(`isOpenContest(contests.find(x=>x.id===${JSON.stringify(open)}))`));
const saved=JSON.parse(w.localStorage.getItem('gewinnen-user-v1'));assert(saved.items[base.id].done);assert.equal(saved.items[base.id].note,'Must remain');
w.__testEval('loadData(true)');await new Promise(resolve=>setTimeout(resolve,100));assert(!w.__testEval(`isOpenContest(contests.find(x=>x.id===${JSON.stringify(open)}))`));
assert(!errors.length,errors.join('\n'));console.log('PASS full app startup; legacy migration; 30→60 paging; More navigation; mark and catalogue reload; personal data retained');dom.window.close();
})().catch(e=>{console.error(e);process.exit(1)});
