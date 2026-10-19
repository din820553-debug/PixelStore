'use strict';
const http=require('node:http');const fs=require('node:fs');const path=require('node:path');
const {randomBytes,randomUUID,timingSafeEqual}=require('node:crypto');
const {openStore,Fault,hash}=require('./store.cjs');
const ROOT=path.resolve(__dirname,'..');
function createApp(options={}){
 const directory=path.resolve(options.dataDir||process.env.DATA_DIR||path.join(ROOT,'data'));
 const store=openStore(directory);store.db.exec('DELETE FROM sessions');
 const adminToken=options.adminToken??process.env.ADMIN_TOKEN??'';
 if(adminToken&&adminToken.length<32)throw new Error('ADMIN_TOKEN must be at least 32 characters');
 const port=options.port??Number(process.env.PORT||3000);
 const publicOrigin=options.publicOrigin||process.env.PUBLIC_ORIGIN||`http://localhost:${port}`;
 const serverOrigin=new URL(publicOrigin).origin;
 const secure=serverOrigin.startsWith('https:');
 const allowed=new Set([serverOrigin,`http://localhost:${port}`,`http://127.0.0.1:${port}`,...String(process.env.ALLOWED_ORIGINS||'https://din820553-debug.github.io').split(',').map(s=>s.trim()).filter(Boolean)]);
 const rates=new Map();
 function limit(req,key,max,period=60000){const k=(req.socket.remoteAddress||'unknown')+':'+key,t=Date.now(),old=rates.get(k);const state=old&&old.until>t?old:{n:0,until:t+period};state.n++;rates.set(k,state);if(rates.size>10000)for(const [key,val] of rates)if(val.until<t)rates.delete(key);if(state.n>max)throw new Fault(429,'Слишком много запросов. Попробуйте позже')}
 function headers(req,res){res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('X-Frame-Options','DENY');res.setHeader('Referrer-Policy','no-referrer');res.setHeader('Cache-Control','no-store');
  if(allowed.has(req.headers.origin)){res.setHeader('Access-Control-Allow-Origin',req.headers.origin);res.setHeader('Vary','Origin');res.setHeader('Access-Control-Allow-Credentials','true')}
 }
 function send(res,status,data){res.writeHead(status,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(data))}
 async function json(req,max=32000){let length=0,chunks=[];for await(const chunk of req){length+=chunk.length;if(length>max)throw new Fault(413,'Слишком большой запрос');chunks.push(chunk)}try{return JSON.parse(Buffer.concat(chunks).toString('utf8'))}catch{throw new Fault(400,'Нужен корректный JSON')}}
 function session(req){const cookie=String(req.headers.cookie||'').split(';').map(s=>s.trim()).find(s=>s.startsWith('ps_admin='));const raw=cookie?.slice(9);if(!raw||!/^[a-f0-9]{64}$/.test(raw))return false;return !!store.db.prepare('SELECT token_hash FROM sessions WHERE token_hash=? AND expires_at>?').get(hash(raw),Date.now())}
 function requireAdmin(req){if(!session(req))throw new Fault(401,'Войдите в кабинет продавца');if(req.headers.origin&&req.headers.origin!==serverOrigin)throw new Fault(403,'Откройте кабинет на адресе сервера')}
 function customerToken(req){return String(req.headers.authorization||'').replace(/^Bearer /,'')}
 function sniff(bytes){if(bytes.subarray(0,5).toString()==='%PDF-')return 'application/pdf';if(bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))return 'image/png';if(bytes[0]===255&&bytes[1]===216&&bytes[2]===255)return 'image/jpeg';if(bytes.subarray(0,4).toString()==='RIFF'&&bytes.subarray(8,12).toString()==='WEBP')return 'image/webp';throw new Fault(400,'Разрешены PDF, JPEG, PNG и WebP')}
 function csv(orders){const cell=v=>{let t=String(v??'');if(/^[=+\-@\t\r]/.test(t))t="'"+t;return '"'+t.replaceAll('"','""')+'"'};return '\uFEFF'+[['Номер','Создан','Никнейм','Режим','Оплата','Выполнение','Сумма ₽','Возврат ₽','Расходы ₽','Остаток ₽','Заметка'],...orders.map(o=>{const cost=Object.values(o.costs).reduce((a,b)=>a+b,0);return [o.number,o.createdAt,o.nickname,o.mode,o.paymentStatus,o.fulfillmentStatus,o.total,o.refundedKopecks/100,cost/100,(o.totalKopecks-o.refundedKopecks-cost)/100,o.note]})].map(r=>r.map(cell).join(';')).join('\r\n')}
 const server=http.createServer(async(req,res)=>{headers(req,res);try{
  const url=new URL(req.url,'http://localhost'),route=url.pathname,method=req.method;
  if(route.startsWith('/api/')){
   if(req.headers.origin&&!allowed.has(req.headers.origin))throw new Fault(403,'Этот сайт не разрешён настройкой сервера');
   if(method==='OPTIONS'){res.writeHead(204,{'Access-Control-Allow-Methods':'GET,POST,PATCH,OPTIONS','Access-Control-Allow-Headers':'Content-Type,Authorization,Idempotency-Key','Access-Control-Max-Age':'600'});return res.end()}
   if(method==='GET'&&route==='/api/health')return send(res,200,{ok:true,storage:'server',paymentsConnected:false});
   if(method==='POST'&&route==='/api/admin/login'){
    if(req.headers.origin&&req.headers.origin!==serverOrigin)throw new Fault(403,'Вход доступен на адресе сервера');
    limit(req,'login',8,15*60000);if(!adminToken)throw new Fault(503,'Задайте ADMIN_TOKEN на сервере');const body=await json(req);
    const provided=typeof body.token==='string'?body.token:'';
    if(!timingSafeEqual(Buffer.from(hash(provided)),Buffer.from(hash(adminToken))))throw new Fault(401,'Неверный ключ продавца');
    const raw=randomBytes(32).toString('hex');store.db.prepare('DELETE FROM sessions WHERE expires_at<?').run(Date.now());store.db.prepare('INSERT INTO sessions VALUES(?,?)').run(hash(raw),Date.now()+12*3600000);
    res.setHeader('Set-Cookie',`ps_admin=${raw}; HttpOnly; SameSite=Strict; Path=/api; Max-Age=43200${secure?'; Secure':''}`);return send(res,200,{ok:true});
   }
   if(route.startsWith('/api/admin/')){
    requireAdmin(req);limit(req,'admin',300);
    if(method==='GET'&&route==='/api/admin/session')return send(res,200,{ok:true});
    if(method==='POST'&&route==='/api/admin/logout'){
     const raw=String(req.headers.cookie||'').split(';').map(s=>s.trim()).find(s=>s.startsWith('ps_admin='))?.slice(9);if(raw)store.db.prepare('DELETE FROM sessions WHERE token_hash=?').run(hash(raw));res.setHeader('Set-Cookie',`ps_admin=; HttpOnly; SameSite=Strict; Path=/api; Max-Age=0${secure?'; Secure':''}`);return send(res,200,{ok:true});
    }
    if(method==='GET'&&route==='/api/admin/orders')return send(res,200,{orders:store.list(Object.fromEntries(url.searchParams))});
    if(method==='POST'&&route==='/api/admin/orders'){const result=store.create(await json(req),req.headers['idempotency-key'],true);return send(res,result.repeated?200:201,result)}
    if(method==='GET'&&route==='/api/admin/export.csv'){res.writeHead(200,{'Content-Type':'text/csv; charset=utf-8','Content-Disposition':'attachment; filename="pixelstore-orders.csv"'});return res.end(csv(store.list()))}
    if(method==='GET'&&route==='/api/admin/backup'){
     const output=path.join(directory,'backup-'+randomUUID()+'.sqlite');try{store.backup(output);res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Length':fs.statSync(output).size,'Content-Disposition':`attachment; filename="pixelstore-backup-${new Date().toISOString().slice(0,10)}.sqlite"`});const stream=fs.createReadStream(output);stream.on('error',()=>res.destroy());stream.once('close',()=>fs.unlink(output,()=>{}));res.once('close',()=>stream.destroy());stream.pipe(res);return}catch(error){if(fs.existsSync(output))fs.unlinkSync(output);throw error}
    }
    let match=route.match(/^\/api\/admin\/orders\/([a-f0-9-]{36})$/);
    if(match&&method==='GET')return send(res,200,{order:store.adminOrder(match[1])});
    if(match&&method==='PATCH')return send(res,200,{order:store.change(match[1],await json(req))});
    match=route.match(/^\/api\/admin\/orders\/([a-f0-9-]{36})\/documents$/);
    if(match&&method==='POST'){
     const body=await json(req,7500000);if(typeof body.base64!=='string'||!body.base64.length||body.base64.length%4!==0||!/^[A-Za-z0-9+/]*={0,2}$/.test(body.base64))throw new Fault(400,'Неверный файл');const data=Buffer.from(body.base64,'base64');if(!data.length||data.length>5*1024*1024)throw new Fault(413,'Файл должен быть не больше 5 МБ');
     return send(res,201,{order:store.addDocument(match[1],{...body,mime:sniff(data)},data)});
    }
    match=route.match(/^\/api\/admin\/documents\/([a-f0-9-]{36})$/);
    if(match&&method==='PATCH')return send(res,200,{order:store.shareDocument(match[1],await json(req))});
    if(match&&method==='GET'){const d=store.document(match[1],true);res.writeHead(200,{'Content-Type':d.mime,'Content-Security-Policy':"sandbox; default-src 'none'",'Content-Disposition':`attachment; filename="document.${d.mime==='application/pdf'?'pdf':d.mime.split('/')[1]}"; filename*=UTF-8''${encodeURIComponent(d.name)}`});return res.end(d.data)}
   }
   if(method==='POST'&&route==='/api/orders'){limit(req,'create',15);const result=store.create(await json(req),req.headers['idempotency-key']);return send(res,result.repeated?200:201,result)}
   let match=route.match(/^\/api\/orders\/([a-f0-9-]{36})$/);
   if(match&&method==='GET'){limit(req,'read',120);return send(res,200,{order:store.customer(match[1],customerToken(req))})}
   match=route.match(/^\/api\/documents\/([a-f0-9-]{36})$/);
   if(match&&method==='GET'){limit(req,'documents',60);const d=store.document(match[1],false,customerToken(req));res.writeHead(200,{'Content-Type':d.mime,'Content-Security-Policy':"sandbox; default-src 'none'",'Content-Disposition':`attachment; filename*=UTF-8''${encodeURIComponent(d.name)}`});return res.end(d.data)}
   throw new Fault(404,'Адрес API не найден');
  }
  if(!['GET','HEAD'].includes(method))throw new Fault(405,'Метод недоступен');
  res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'");
  if(route==='/api-config.js'){res.setHeader('Content-Type','application/javascript; charset=utf-8');return res.end('window.PIXELSTORE_API_BASE=location.origin;')}
  const relative=route==='/'?'index.html':(route==='/admin'||route==='/admin/')?'admin.html':decodeURIComponent(route.slice(1));
  const publicFiles=new Set(['index.html','style.css','app.js','catalog.js','orders.js','history.js','admin.html','admin.css','admin.js']);
  const target=path.resolve(ROOT,relative);
  if(!publicFiles.has(relative)&&!(relative.startsWith('assets/')&&target.startsWith(path.join(ROOT,'assets')+path.sep)))throw new Fault(404,'Страница не найдена');
  if(!fs.existsSync(target)||!fs.statSync(target).isFile())throw new Fault(404,'Страница не найдена');
  const mimes={'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.webp':'image/webp','.png':'image/png','.svg':'image/svg+xml'};
  res.setHeader('Content-Type',mimes[path.extname(target)]||'application/octet-stream');res.statusCode=200;if(method==='HEAD')return res.end();fs.createReadStream(target).pipe(res);
 }catch(e){if(res.headersSent){res.end();return}if(!e.status)console.error('Request failed:',e.name);send(res,e.status||500,{error:e.status?e.message:'Ошибка сервера. Повторите запрос позже'})}});
 server.requestTimeout=30000;server.headersTimeout=15000;
 return {server,store,close:()=>new Promise(resolve=>server.close(()=>{store.close();resolve()}))};
}
if(require.main===module){const port=Number(process.env.PORT||3000),host=process.env.HOST||'127.0.0.1';const app=createApp({port});app.server.listen(port,host,()=>console.log(`PixelStore: http://${host}:${port}; seller: /admin; payments not connected`));for(const sig of ['SIGINT','SIGTERM'])process.on(sig,()=>app.close().then(()=>process.exit(0)))}
module.exports={createApp};
