'use strict';
const {DatabaseSync}=require('node:sqlite');
const {randomUUID,randomBytes,createHash}=require('node:crypto');
const fs=require('node:fs');const path=require('node:path');
const catalog=require('../catalog.js');
const hash=s=>createHash('sha256').update(s).digest('hex');
const now=()=>new Date().toISOString();
class Fault extends Error{constructor(status,message){super(message);this.status=status}}
const fail=(status,message)=>{throw new Fault(status,message)};
const text=(s,max=2000)=>{if(typeof s!=='string'||s.length>max)fail(400,'Недопустимый текст');return s.trim()};
const integer=(v,max=100000000)=>{if(!Number.isSafeInteger(v)||v<0||v>max)fail(400,'Сумма должна быть целым числом копеек');return v};
const productPrice=p=>Math.round(p.usd*100*(p.category==='ranks'?1.05:1))*100;
function openStore(directory){
 fs.mkdirSync(directory,{recursive:true,mode:0o700});
 const filename=path.join(directory,'pixelstore.sqlite');const db=new DatabaseSync(filename);
 fs.chmodSync(filename,0o600);db.exec('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;');
 if(db.prepare('PRAGMA user_version').get().user_version>1)throw Error('Database version is newer than this server');
 db.exec(fs.readFileSync(path.join(__dirname,'schema.sql'),'utf8'));
 function transact(fn){db.exec('BEGIN IMMEDIATE');try{const r=fn();db.exec('COMMIT');return r}catch(e){db.exec('ROLLBACK');throw e}}
 function event(id,type,details,actor='seller'){db.prepare('INSERT INTO events VALUES (?,?,?,?,?,?)').run(randomUUID(),id,now(),actor,type,JSON.stringify(details))}
 function row(id){const o=db.prepare('SELECT * FROM orders WHERE id=?').get(id);if(!o)fail(404,'Заказ не найден');return o}
 function serialize(o,admin=false){
  const items=db.prepare('SELECT product_id,name,quantity,unit_kop FROM order_items WHERE order_id=?').all(o.id).map(i=>({id:i.product_id,name:i.name,qty:i.quantity,unitPriceKopecks:i.unit_kop,unitPrice:i.unit_kop/100,subtotalKopecks:i.quantity*i.unit_kop}));
  const documents=db.prepare('SELECT id,kind,name,mime,size,sha256,shared,created_at FROM documents WHERE order_id=?'+(admin?'':' AND shared=1')).all(o.id).map(d=>({...d,shared:!!d.shared,createdAt:d.created_at}));
  const result={id:o.id,number:o.number,createdAt:o.created_at,updatedAt:o.updated_at,nickname:o.nickname,totalKopecks:o.total_kop,total:o.total_kop/100,currency:o.currency,mode:o.mode,demo:o.mode==='demo',mvpPlusConfirmed:!!o.mvp_confirmed,paymentStatus:o.payment_status,fulfillmentStatus:o.fulfillment_status,refundedKopecks:o.refunded_kop,version:o.version,items,documents};
  if(admin){result.costs=JSON.parse(o.costs_json);result.note=o.note;result.events=db.prepare('SELECT id,created_at,actor,type,details_json FROM events WHERE order_id=? ORDER BY rowid').all(o.id).map(e=>({id:e.id,createdAt:e.created_at,actor:e.actor,type:e.type,details:JSON.parse(e.details_json)}));}
  return result;
 }
 function create(body,key,admin=false){
  if(typeof key!=='string'||! /^[a-zA-Z0-9_-]{16,100}$/.test(key))fail(400,'Нужен ключ повторного запроса');
  if(!body||typeof body!=='object')fail(400,'Нет данных заказа');
  const nickname=text(body.nickname,16);if(!/^[A-Za-z0-9_]{3,16}$/.test(nickname))fail(400,'Проверьте никнейм Minecraft');
  if(!Array.isArray(body.items)||!body.items.length||body.items.length>10)fail(400,'Неверный состав заказа');
  const accessToken=admin?(body.accessToken||randomBytes(32).toString('hex')):body.accessToken;
  if(typeof accessToken!=='string'||!/^[a-f0-9]{64}$/.test(accessToken))fail(400,'Недопустимый ключ доступа');
  const mode=admin?'manual':'demo';
  const rawItems=body.items.map(i=>{if(typeof i?.id!=='string'||!Number.isInteger(i.qty)||i.qty<1||i.qty>99)fail(400,'Недопустимый товар или количество');return {id:i.id,qty:i.qty}});
  const reqHash=hash(JSON.stringify({nickname,items:rawItems,confirmed:body.mvpPlusConfirmed===true,accessToken,mode,expectedTotalKopecks:admin?null:body.expectedTotalKopecks}));
  const previous=db.prepare('SELECT * FROM orders WHERE idempotency_key=?').get(key);
  if(previous){if(previous.request_hash!==reqHash)fail(409,'Ключ запроса уже использован с другим заказом');return {order:serialize(previous,admin),accessToken,repeated:true}}
  const seen=new Set();const items=rawItems.map(i=>{const p=catalog.find(p=>p.id===i.id);if(!p||seen.has(i.id))fail(400,'Недопустимый товар или количество');seen.add(i.id);return {id:p.id,name:p.name,qty:i.qty,unit:productPrice(p)}});
  if(seen.has('mvpplusplus')&&body.mvpPlusConfirmed!==true)fail(400,'Подтвердите наличие MVP+ у получателя');
  const total=items.reduce((s,i)=>s+i.qty*i.unit,0);integer(total,50000000);if(!admin&&body.expectedTotalKopecks!==total)fail(409,'Цена изменилась. Обновите каталог перед оформлением');
  return transact(()=>{const id=randomUUID(),stamp=now(),number='PS-'+stamp.slice(0,10).replaceAll('-','')+'-'+id.slice(0,8).toUpperCase();
   db.prepare('INSERT INTO orders(id,number,created_at,updated_at,nickname,total_kop,mode,mvp_confirmed,access_hash,idempotency_key,request_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)').run(id,number,stamp,stamp,nickname,total,mode,body.mvpPlusConfirmed===true?1:0,hash(accessToken),key,reqHash);
   const stmt=db.prepare('INSERT INTO order_items VALUES(?,?,?,?,?)');for(const i of items)stmt.run(id,i.id,i.name,i.qty,i.unit);
   event(id,'order.created',{mode,totalKopecks:total},admin?'seller':'customer');return {order:serialize(row(id),admin),accessToken,repeated:false};
  });
 }
 function customer(id,token){const o=row(id);if(typeof token!=='string'||hash(token)!==o.access_hash)fail(404,'Заказ не найден или неверный ключ');return serialize(o)}
 function adminOrder(id){return serialize(row(id),true)}
 function list(query={}){const all=db.prepare('SELECT * FROM orders ORDER BY created_at DESC').all();const q=String(query.q||'').toLowerCase().slice(0,100);return all.filter(o=>(!q||(o.number+' '+o.nickname).toLowerCase().includes(q))&&(!query.payment||o.payment_status===query.payment)&&(!query.fulfillment||o.fulfillment_status===query.fulfillment)).map(o=>serialize(o,true))}
 function change(id,body){return transact(()=>{const o=row(id);if(body.version!==o.version)fail(409,'Заказ изменён. Обновите страницу');
  if(body.action==='payment'){
   if(!['paid','refunded'].includes(body.status))fail(400,'Недопустимый платёжный статус');
   if(body.status==='paid'&&o.payment_status!=='unpaid')fail(409,'Оплата уже отмечена');
   if(body.status==='paid'&&o.fulfillment_status==='cancelled')fail(409,'Отменённый заказ нельзя отметить оплаченным');
   if(body.status==='refunded'&&o.payment_status!=='paid')fail(409,'Нужна оплаченная покупка');
   const reference=text(body.reference,200);if(reference.length<3)fail(400,'Укажите подтверждение ручной сверки');
   if(integer(body.amountKopecks)!==o.total_kop)fail(400,'Сумма не совпадает с заказом; частичные платежи не поддерживаются');
   db.prepare('UPDATE orders SET payment_status=?,refunded_kop=? WHERE id=?').run(body.status,body.status==='refunded'?o.total_kop:0,id);
   event(id,'payment.manual_'+body.status,{reference,amountKopecks:o.total_kop,mode:o.mode});
  }else if(body.action==='fulfillment'){
   const transitions={new:['processing','cancelled'],processing:['completed','cancelled'],completed:[],cancelled:[]};
   if(!transitions[o.fulfillment_status].includes(body.status))fail(409,'Такой переход выполнения недоступен');
   if(['processing','completed'].includes(body.status)&&o.payment_status!=='paid')fail(409,'Сначала нужно подтвердить оплату');
   const reference=text(body.reference||'',200);if(body.status==='completed'&&reference.length<3)fail(400,'Укажите подтверждение покупки у поставщика');
   db.prepare('UPDATE orders SET fulfillment_status=? WHERE id=?').run(body.status,id);event(id,'fulfillment.changed',{from:o.fulfillment_status,to:body.status,reference});
  }else if(body.action==='costs'){
   const keys=['purchase','cardFee','processingFee','withdrawalFee','tax','other'];const costs={};for(const k of keys)costs[k]=integer(body.costs?.[k]??0);
   db.prepare('UPDATE orders SET costs_json=? WHERE id=?').run(JSON.stringify(costs),id);event(id,'costs.changed',{before:JSON.parse(o.costs_json),after:costs});
  }else if(body.action==='note'){
   const note=text(body.note||'',2000);db.prepare('UPDATE orders SET note=? WHERE id=?').run(note,id);event(id,'note.changed',{before:o.note,after:note});
  }else fail(400,'Неизвестное действие');
  db.prepare('UPDATE orders SET version=version+1,updated_at=? WHERE id=?').run(now(),id);return serialize(row(id),true);
 })}
 function addDocument(id,meta,data){return transact(()=>{row(id);const kind=meta.kind;if(!['supplier','payment','fiscal','other'].includes(kind))fail(400,'Неизвестный вид документа');
  if(db.prepare('SELECT count(*) n FROM documents WHERE order_id=?').get(id).n>=20)fail(400,'Лимит 20 документов на заказ');
  const docId=randomUUID(),name=text(meta.name,120).replace(/[\x00-\x1f<>:"/\\|?*]/g,'_')||'document';
  db.prepare('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)').run(docId,id,now(),kind,name,meta.mime,data.length,hash(data),meta.shared===true?1:0,data);
  event(id,'document.added',{id:docId,name,kind,shared:meta.shared===true,sha256:hash(data)});db.prepare('UPDATE orders SET version=version+1,updated_at=? WHERE id=?').run(now(),id);return serialize(row(id),true);
 })}
 function shareDocument(id,body){return transact(()=>{const d=db.prepare('SELECT id,order_id,shared FROM documents WHERE id=?').get(id);if(!d)fail(404,'Документ не найден');const o=row(d.order_id);if(body.version!==o.version)fail(409,'Заказ изменён. Обновите страницу');if(typeof body.shared!=='boolean')fail(400,'Укажите доступ к документу');db.prepare('UPDATE documents SET shared=? WHERE id=?').run(body.shared?1:0,id);event(o.id,'document.access_changed',{id,from:!!d.shared,to:body.shared});db.prepare('UPDATE orders SET version=version+1,updated_at=? WHERE id=?').run(now(),o.id);return serialize(row(o.id),true)})}
 function document(id,admin=false,token){const d=db.prepare('SELECT * FROM documents WHERE id=?').get(id);if(!d)fail(404,'Документ не найден');if(!admin){customer(d.order_id,token);if(!d.shared)fail(404,'Документ не найден')}return d}
 function backup(output){db.exec("VACUUM INTO '"+output.replaceAll("'","''")+"'");fs.chmodSync(output,0o600)}
 return {db,filename,create,customer,adminOrder,list,change,addDocument,shareDocument,document,backup,close:()=>db.close()};
}
module.exports={openStore,Fault,hash};
