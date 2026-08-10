const CACHE='win-win-7.3';
const CORE=[
 './',
 './index.html',
 './styles.css',
 './app.js',
 './manifest.webmanifest',
 './version.json',
 './apple-touch-icon.png',
 './icons/icon-180.png',
 './icons/icon-192.png',
 './icons/icon-512.png'
];
const DATA_PATHS=['./contests.json','./data/sources.json','./sources.json'];

self.addEventListener('install',event=>{
 event.waitUntil((async()=>{
  const cache=await caches.open(CACHE);
  await cache.addAll(CORE);
  await Promise.allSettled(
   DATA_PATHS.map(path=>
    fetch(path,{cache:'no-store'})
     .then(response=>response.ok?cache.put(path,response):null)
   )
  );
 })());
 self.skipWaiting();
});

self.addEventListener('activate',event=>{
 event.waitUntil((async()=>{
  const keys=await caches.keys();
  await Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)));
  await self.clients.claim();
 })());
});

self.addEventListener('fetch',event=>{
 if(event.request.method!=='GET')return;

 const url=new URL(event.request.url);
 const isNavigation=event.request.mode==='navigate';
 const isJson=url.pathname.endsWith('.json');
 const isScript=event.request.destination==='script';
 const isStyle=event.request.destination==='style';

 if(isNavigation){
  event.respondWith((async()=>{
   try{
    const response=await fetch(event.request,{cache:'no-store'});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const cache=await caches.open(CACHE);
    cache.put('./index.html',response.clone());
    return response;
   }catch(error){
    return (await caches.match('./index.html')) ||
      new Response('Win Win ist momentan offline.',{
       status:503,
       headers:{'Content-Type':'text/plain; charset=utf-8'}
      });
   }
  })());
  return;
 }

 if(isJson){
  event.respondWith((async()=>{
   try{
    const response=await fetch(event.request,{cache:'no-store'});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const cache=await caches.open(CACHE);
    cache.put(event.request,response.clone());
    return response;
   }catch(error){
    return (await caches.match(event.request,{ignoreSearch:true})) ||
      new Response(JSON.stringify({error:'Datei nicht verfügbar'}),{
       status:404,
       headers:{'Content-Type':'application/json'}
      });
   }
  })());
  return;
 }

 // Critical rule: JS and CSS must never receive index.html as fallback.
 if(isScript||isStyle){
  event.respondWith((async()=>{
   try{
    const response=await fetch(event.request,{cache:'no-store'});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const cache=await caches.open(CACHE);
    cache.put(event.request,response.clone());
    return response;
   }catch(error){
    return (await caches.match(event.request,{ignoreSearch:true})) ||
      new Response('',{
       status:504,
       statusText:'Asset nicht verfügbar'
      });
   }
  })());
  return;
 }

 event.respondWith((async()=>{
  try{
   const response=await fetch(event.request);
   if(response.ok){
    const cache=await caches.open(CACHE);
    cache.put(event.request,response.clone());
   }
   return response;
  }catch(error){
   return (await caches.match(event.request,{ignoreSearch:true})) ||
     new Response('',{status:504,statusText:'Offline'});
  }
 })());
});
