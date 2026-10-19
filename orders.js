/* Order snapshots and access keys stay in this browser; no payment credentials belong here. */
(function(){
'use strict';
const KEY='pixel-orders-v1';let pendingMemory=null;
const random=()=>Array.from(crypto.getRandomValues(new Uint8Array(32)),n=>n.toString(16).padStart(2,'0')).join('');
const apiBase=String(window.PIXELSTORE_API_BASE||'').replace(/\/$/,'');
if(apiBase&&!/^https:\/\//.test(apiBase)&&!/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(apiBase))throw Error('Адрес сервера должен использовать HTTPS');
function records(){try{const data=JSON.parse(localStorage.getItem(KEY)||'[]');return Array.isArray(data)?data.filter(x=>x?.order?.id&&Array.isArray(x.order.items)):[]}catch{return []}}
function put(record){const list=records();const i=list.findIndex(x=>x.order.id===record.order.id);if(i>=0)list[i]=record;else list.unshift(record);localStorage.setItem(KEY,JSON.stringify(list));return record}
async function request(base,path,options={}){const response=await fetch(base+path,{...options,cache:'no-store'});if(!response.ok){const data=await response.json().catch(()=>({}));throw Error(data.error||'Сервер недоступен. Повторите запрос')}return response.json()}
function snapshot(input){const map=window.PIXELSTORE_CATALOG;const items=input.items.map(i=>{const p=map.find(p=>p.id===i.id);if(!p)throw Error('Товар больше не доступен');const unit=Math.round(p.usd*100*(p.category==='ranks'?1.05:1))*100;return {id:p.id,name:p.name,qty:i.qty,unitPriceKopecks:unit,unitPrice:unit/100,subtotalKopecks:unit*i.qty}});const id=crypto.randomUUID(),date=new Date().toISOString();const totalKopecks=items.reduce((s,i)=>s+i.subtotalKopecks,0);return {id,number:'LOCAL-'+id.slice(0,8).toUpperCase(),createdAt:date,updatedAt:date,nickname:input.nickname,items,totalKopecks,total:totalKopecks/100,currency:'RUB',mvpPlusConfirmed:input.mvpPlusConfirmed===true,mode:'demo',demo:true,paymentStatus:'unpaid',fulfillmentStatus:'new',refundedKopecks:0,documents:[],version:1}}
async function create(input){
 const local=snapshot(input);let record;
 if(apiBase){
  const fingerprint=JSON.stringify({nickname:input.nickname,items:input.items,mvpPlusConfirmed:input.mvpPlusConfirmed===true,total:local.totalKopecks});let draft=pendingMemory;
  try{draft=JSON.parse(sessionStorage.getItem('pixel-pending-order')||'null')||pendingMemory}catch{}
  if(!draft||draft.fingerprint!==fingerprint||draft.apiBase!==apiBase){draft={fingerprint,apiBase,idempotencyKey:random(),token:random()};pendingMemory=draft;try{sessionStorage.setItem('pixel-pending-order',JSON.stringify(draft))}catch{}}
  const result=await request(apiBase,'/api/orders',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':draft.idempotencyKey},body:JSON.stringify({...input,accessToken:draft.token,expectedTotalKopecks:local.totalKopecks})});
  record={order:result.order,apiBase,accessToken:draft.token,remote:true};
  // Do not silently discard access if browser storage is blocked after server creation.
  pendingMemory=null;try{sessionStorage.removeItem('pixel-pending-order')}catch{}
  let saved=true;try{put(record)}catch{saved=false}
  return {record,saved};
 }
 record={order:local,remote:false};let saved=true;try{put(record)}catch{saved=false}return {record,saved};
}
async function refresh(record){if(!record.remote)return record;const result=await request(record.apiBase,'/api/orders/'+encodeURIComponent(record.order.id),{headers:{Authorization:'Bearer '+record.accessToken}});const updated={...record,order:result.order};put(updated);return updated}
async function importOrder(id,token){if(!apiBase)throw Error('Сервер ещё не подключён');if(!/^[a-f0-9-]{36}$/.test(id)||! /^[a-f0-9]{64}$/.test(token))throw Error('Проверьте идентификатор и ключ заказа');return refresh({order:{id},accessToken:token,apiBase,remote:true})}
function receipt(record){const o=record.order;const money=n=>new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:2}).format(n/100);const lines=['PIXELSTORE — ОПИСАНИЕ ЗАКАЗА','Не является фискальным чеком.','Номер: '+o.number,'Создан: '+o.createdAt,'Получатель: '+o.nickname,'Режим: '+(o.demo?'Демонстрация — реальная оплата не выполнялась':'Ручной учёт продавцом'),...o.items.map(i=>`${i.name} × ${i.qty} — ${money(i.unitPriceKopecks*i.qty)}`),'Итого: '+money(o.totalKopecks),'Статус оплаты: '+({unpaid:'Не оплачено',paid:'Оплата отмечена продавцом вручную',refunded:'Возврат отмечен продавцом вручную'}[o.paymentStatus]||o.paymentStatus),'Выполнение: '+({new:'Создан',processing:'В работе',completed:'Выполнен',cancelled:'Отменён'}[o.fulfillmentStatus]||o.fulfillmentStatus)];if(o.mvpPlusConfirmed)lines.push('Покупатель подтвердил наличие MVP+ на аккаунте получателя.');if(record.remote)lines.push('Идентификатор: '+o.id,'Ключ доступа к этому заказу: '+record.accessToken,'Не публикуйте ключ: он открывает доступ к заказу и документам, разрешённым продавцом.');else lines.push('Локальная демонстрация: этот заказ не отправлен продавцу.');return lines.join('\n')}
function downloadText(text,filename){const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
async function downloadDocument(record,id){const d=record.order.documents?.find(d=>d.id===id);if(!d)throw Error('Документ недоступен');const response=await fetch(record.apiBase+'/api/documents/'+encodeURIComponent(id),{headers:{Authorization:'Bearer '+record.accessToken},cache:'no-store'});if(!response.ok)throw Error('Не удалось получить документ');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=d.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
window.PixelOrders={records,create,refresh,importOrder,receipt,downloadText,downloadDocument,apiBase};
})();
