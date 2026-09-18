const vm=require('vm'),fs=require('fs'),assert=require('assert');
const source=fs.readFileSync('app.js','utf8');
function fn(name){const a=source.indexOf('function '+name+'('),b=source.indexOf('\nfunction ',a+10);assert(a>=0);return source.slice(a,b)}
function element(id){return {id,dataset:{view:id},attrs:{},classList:{active:false,toggle(k,v){this[k]=v}},setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]}}}
const views=['homeView','todayView','discoverView','favoritesView','moreView','statsView','dataView'].map(element),nav=['discoverView','favoritesView','moreView'].map(element);
const c={console,window:{scrollTo(){}},$$:s=>s==='.view'?views:nav,currentFilter:'all',advancedFilters:{},$:(s)=>s==='#searchInput'?{value:''}:{value:'score'},scored:()=>c.rows,matches:()=>true,passesAdvancedFilters:()=>true,isOpenContest:i=>!i.done&&!i.ignored,daysLeft:i=>i.days,normalizedSearchText:i=>i.title};vm.createContext(c);
vm.runInContext(fn('openView'),c);vm.runInContext(fn('discoverItems'),c);
for(const id of ['homeView','todayView','discoverView','favoritesView','moreView','statsView','dataView']){c.openView(id);const target=['homeView','todayView'].includes(id)?'discoverView':id;assert.deepEqual(views.filter(v=>v.classList.active).map(v=>v.id),[target]);assert.equal(nav.filter(n=>n.attrs['aria-current']==='page').length,1)}
c.rows=[{id:'open',score:3},{id:'done',done:true,score:4},{id:'ignored',ignored:true,score:5}];assert.deepEqual(Array.from(c.discoverItems(),i=>i.id),['open']);
console.log('PASS: legacy routes reach unified list; exactly one selected tab; done and ignored stay out of open list');
const html=fs.readFileSync('index.html','utf8');assert.equal((html.match(/class="nav-item/g)||[]).length,3);assert(html.includes('id="moreView"'));assert(html.includes('id="discoverView" class="view active"'));
