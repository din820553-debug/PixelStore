"""SQLite persistence. No customer document is written into the public web root."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from hashlib import sha256
from pathlib import Path
from threading import RLock
import hmac
import json
import re
import secrets
import shutil
import sqlite3
import uuid

ROOT = Path(__file__).resolve().parents[1]

def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

def digest(value):
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()

class Fault(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status

def require(condition, status, message):
    if not condition:
        raise Fault(status, message)

def text(value, maximum=2000):
    require(isinstance(value, str) and len(value) <= maximum, 400, 'Недопустимый текст')
    return value.strip()

def amount(value, maximum=100_000_000):
    require(type(value) is int and 0 <= value <= maximum, 400, 'Сумма должна быть целым числом копеек')
    return value

def guarded(method):
    @wraps(method)
    def inner(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return inner

class Store:
    def __init__(self, directory, catalog=None, max_data_mb=300, min_free_mb=50):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.filename = self.directory / 'pixelstore.sqlite'
        self.lock = RLock()
        self.max_bytes = int(max_data_mb) * 1024 * 1024
        self.min_free = int(min_free_mb) * 1024 * 1024
        self.catalog = catalog if catalog is not None else json.loads((ROOT / 'catalog.json').read_text())
        self.db = sqlite3.connect(self.filename, timeout=5, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.filename.chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=5000')
        require(self.db.execute('PRAGMA user_version').fetchone()[0] <= 1, 500, 'База создана более новой версией сервера')
        self.db.executescript((ROOT / 'server/schema.sql').read_text())

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def usage(self):
        return sum(p.stat().st_size for p in self.directory.glob('pixelstore.sqlite*') if p.is_file())

    def capacity(self, extra=0):
        require(self.usage() + extra <= self.max_bytes, 507, 'Лимит базы исчерпан. Скачайте резервную копию и увеличьте хранилище')
        require(shutil.disk_usage(self.directory).free >= extra + self.min_free, 507, 'Недостаточно свободного места на диске')

    def row(self, order_id):
        result = self.db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        require(result is not None, 404, 'Заказ не найден')
        return result

    def event(self, order_id, kind, details, actor='seller'):
        self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (str(uuid.uuid4()), order_id, stamp(), actor, kind, json.dumps(details, ensure_ascii=False)))

    def touch(self, order_id):
        self.db.execute('UPDATE orders SET version=version+1,updated_at=? WHERE id=?', (stamp(), order_id))

    def serialize(self, row, admin=False):
        items = [dict(id=i['product_id'], name=i['name'], qty=i['quantity'], unitPriceKopecks=i['unit_kop'], unitPrice=i['unit_kop']/100, subtotalKopecks=i['quantity']*i['unit_kop']) for i in self.db.execute('SELECT product_id,name,quantity,unit_kop FROM order_items WHERE order_id=?', (row['id'],))]
        docs = []
        for d in self.db.execute('SELECT id,kind,name,mime,size,sha256,shared,created_at FROM documents WHERE order_id=?' + ('' if admin else ' AND shared=1'), (row['id'],)):
            docs.append(dict(d) | {'shared': bool(d['shared']), 'createdAt': d['created_at']})
        result = dict(id=row['id'], number=row['number'], createdAt=row['created_at'], updatedAt=row['updated_at'], nickname=row['nickname'], totalKopecks=row['total_kop'], total=row['total_kop']/100, currency=row['currency'], mode=row['mode'], demo=row['mode']=='demo', mvpPlusConfirmed=bool(row['mvp_confirmed']), paymentStatus=row['payment_status'], fulfillmentStatus=row['fulfillment_status'], refundedKopecks=row['refunded_kop'], version=row['version'], items=items, documents=docs)
        if admin:
            result.update(costs=json.loads(row['costs_json']), note=row['note'])
            result['events'] = [dict(id=e['id'], createdAt=e['created_at'], actor=e['actor'], type=e['type'], details=json.loads(e['details_json'])) for e in self.db.execute('SELECT * FROM events WHERE order_id=? ORDER BY rowid', (row['id'],))]
        return result

    @guarded
    def create(self, body, key, admin=False):
        require(isinstance(key, str) and re.fullmatch(r'[a-zA-Z0-9_-]{16,100}', key), 400, 'Нужен ключ повторного запроса')
        require(isinstance(body, dict), 400, 'Нет данных заказа')
        nickname = text(body.get('nickname'), 16)
        require(re.fullmatch(r'[A-Za-z0-9_]{3,16}', nickname), 400, 'Проверьте никнейм Minecraft')
        raw = body.get('items')
        require(isinstance(raw, list) and 1 <= len(raw) <= 10, 400, 'Неверный состав заказа')
        token = (body.get('accessToken') or secrets.token_hex(32)) if admin else body.get('accessToken')
        require(isinstance(token, str) and re.fullmatch(r'[a-f0-9]{64}', token), 400, 'Недопустимый ключ доступа')
        raw_items = []
        for item in raw:
            require(isinstance(item, dict) and isinstance(item.get('id'), str) and type(item.get('qty')) is int and 1 <= item['qty'] <= 99, 400, 'Недопустимый товар или количество')
            raw_items.append(dict(id=item['id'], qty=item['qty']))
        mode = 'manual' if admin else 'demo'
        canonical = dict(nickname=nickname, items=raw_items, confirmed=body.get('mvpPlusConfirmed') is True, accessToken=token, mode=mode, expectedTotalKopecks=None if admin else body.get('expectedTotalKopecks'))
        request_hash = digest(json.dumps(canonical, ensure_ascii=False, separators=(',', ':')))
        with self.transaction():
            old = self.db.execute('SELECT * FROM orders WHERE idempotency_key=?', (key,)).fetchone()
            if old:
                require(hmac.compare_digest(old['request_hash'], request_hash), 409, 'Ключ запроса уже использован с другим заказом')
                return dict(order=self.serialize(old, admin), accessToken=token, repeated=True)
            seen, items = set(), []
            for item in raw_items:
                product = next((p for p in self.catalog if p['id']==item['id']), None)
                require(product and item['id'] not in seen, 400, 'Недопустимый товар или количество')
                seen.add(item['id'])
                unit = int((Decimal(str(product['usd']))*100*(Decimal('1.05') if product['category']=='ranks' else 1)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))*100
                items.append(dict(id=item['id'], name=product['name'], qty=item['qty'], unit=unit))
            require('mvpplusplus' not in seen or body.get('mvpPlusConfirmed') is True, 400, 'Подтвердите наличие MVP+ у получателя')
            total = sum(i['unit']*i['qty'] for i in items)
            amount(total, 50_000_000)
            require(admin or (type(body.get('expectedTotalKopecks')) is int and body['expectedTotalKopecks']==total), 409, 'Цена изменилась. Обновите каталог перед оформлением')
            self.capacity(16384)
            order_id, created = str(uuid.uuid4()), stamp()
            number = 'PS-' + created[:10].replace('-', '') + '-' + order_id[:8].upper()
            self.db.execute('INSERT INTO orders(id,number,created_at,updated_at,nickname,total_kop,mode,mvp_confirmed,access_hash,idempotency_key,request_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (order_id, number, created, created, nickname, total, mode, int(body.get('mvpPlusConfirmed') is True), digest(token), key, request_hash))
            self.db.executemany('INSERT INTO order_items VALUES(?,?,?,?,?)', [(order_id,i['id'],i['name'],i['qty'],i['unit']) for i in items])
            self.event(order_id, 'order.created', dict(mode=mode,totalKopecks=total), 'seller' if admin else 'customer')
            return dict(order=self.serialize(self.row(order_id), admin), accessToken=token, repeated=False)

    @guarded
    def customer(self, order_id, token):
        row = self.row(order_id)
        require(isinstance(token, str) and hmac.compare_digest(digest(token), row['access_hash']), 404, 'Заказ не найден или неверный ключ')
        return self.serialize(row)

    @guarded
    def admin_order(self, order_id):
        return self.serialize(self.row(order_id), True)

    @guarded
    def list(self, query=None):
        query = query or {}
        q = str(query.get('q', '')).lower()[:100]
        return [self.serialize(o, True) for o in self.db.execute('SELECT * FROM orders ORDER BY created_at DESC').fetchall() if (not q or q in (o['number']+' '+o['nickname']).lower()) and (not query.get('payment') or o['payment_status']==query['payment']) and (not query.get('fulfillment') or o['fulfillment_status']==query['fulfillment'])]

    @guarded
    def change(self, order_id, body):
        require(isinstance(body, dict),400,'Нет данных изменения')
        with self.transaction():
            o = self.row(order_id)
            require(type(body.get('version')) is int and body['version']==o['version'],409,'Заказ изменён. Обновите страницу')
            self.capacity(8192)
            action = body.get('action')
            if action == 'payment':
                status = body.get('status')
                require(status in ('paid','refunded'),400,'Недопустимый платёжный статус')
                require((status=='paid' and o['payment_status']=='unpaid' and o['fulfillment_status']!='cancelled') or (status=='refunded' and o['payment_status']=='paid'),409,'Этот переход оплаты недоступен')
                reference = text(body.get('reference'),200)
                require(len(reference)>=3,400,'Укажите подтверждение ручной сверки')
                require(amount(body.get('amountKopecks'))==o['total_kop'],400,'Сумма не совпадает с заказом; частичные платежи не поддерживаются')
                self.db.execute('UPDATE orders SET payment_status=?,refunded_kop=? WHERE id=?',(status,o['total_kop'] if status=='refunded' else 0,order_id))
                self.event(order_id,'payment.manual_'+status,dict(reference=reference,amountKopecks=o['total_kop'],mode=o['mode']))
            elif action == 'fulfillment':
                transitions = dict(new=['processing','cancelled'],processing=['completed','cancelled'],completed=[],cancelled=[])
                status = body.get('status')
                require(status in transitions.get(o['fulfillment_status'],[]),409,'Такой переход выполнения недоступен')
                require(status not in ('processing','completed') or o['payment_status']=='paid',409,'Сначала нужно подтвердить оплату')
                reference = text(body.get('reference',''),200)
                require(status!='completed' or len(reference)>=3,400,'Укажите подтверждение покупки у поставщика')
                self.db.execute('UPDATE orders SET fulfillment_status=? WHERE id=?',(status,order_id))
                self.event(order_id,'fulfillment.changed',{'from':o['fulfillment_status'],'to':status,'reference':reference})
            elif action == 'costs':
                values = body.get('costs',{})
                require(isinstance(values,dict),400,'Недопустимые расходы')
                costs = {k:amount(values.get(k,0)) for k in ['purchase','cardFee','processingFee','withdrawalFee','tax','other']}
                self.db.execute('UPDATE orders SET costs_json=? WHERE id=?',(json.dumps(costs),order_id))
                self.event(order_id,'costs.changed',dict(before=json.loads(o['costs_json']),after=costs))
            elif action == 'note':
                note = text(body.get('note',''))
                self.db.execute('UPDATE orders SET note=? WHERE id=?',(note,order_id))
                self.event(order_id,'note.changed',dict(before=o['note'],after=note))
            else:
                raise Fault(400,'Неизвестное действие')
            self.touch(order_id)
            return self.serialize(self.row(order_id), True)

    @guarded
    def add_document(self, order_id, meta, data):
        with self.transaction():
            self.row(order_id)
            kind = meta.get('kind')
            require(kind in ('supplier','payment','fiscal','other'),400,'Неизвестный вид документа')
            require(self.db.execute('SELECT count(*) FROM documents WHERE order_id=?',(order_id,)).fetchone()[0]<20,400,'Лимит 20 документов на заказ')
            require(0<len(data)<=5*1024*1024,413,'Файл должен быть не больше 5 МБ')
            self.capacity(len(data)*3+16384)
            name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', text(meta.get('name'),120)) or 'document'
            doc_id = str(uuid.uuid4())
            self.db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)',(doc_id,order_id,stamp(),kind,name,meta['mime'],len(data),digest(data),int(meta.get('shared') is True),data))
            self.event(order_id,'document.added',dict(id=doc_id,name=name,kind=kind,shared=meta.get('shared') is True,sha256=digest(data)))
            self.touch(order_id)
            return self.serialize(self.row(order_id),True)

    @guarded
    def share_document(self, doc_id, body):
        require(isinstance(body,dict),400,'Нет данных изменения')
        with self.transaction():
            doc = self.db.execute('SELECT id,order_id,shared FROM documents WHERE id=?',(doc_id,)).fetchone()
            require(doc is not None,404,'Документ не найден')
            o = self.row(doc['order_id'])
            require(type(body.get('version')) is int and body['version']==o['version'],409,'Заказ изменён. Обновите страницу')
            require(type(body.get('shared')) is bool,400,'Укажите доступ к документу')
            self.db.execute('UPDATE documents SET shared=? WHERE id=?',(int(body['shared']),doc_id))
            self.event(o['id'],'document.access_changed',{'id':doc_id,'from':bool(doc['shared']),'to':body['shared']})
            self.touch(o['id'])
            return self.serialize(self.row(o['id']),True)

    @guarded
    def document(self, doc_id, admin=False, token=None):
        doc = self.db.execute('SELECT * FROM documents WHERE id=?',(doc_id,)).fetchone()
        require(doc is not None,404,'Документ не найден')
        if not admin:
            self.customer(doc['order_id'],token)
            require(doc['shared'],404,'Документ не найден')
        return dict(doc)

    @guarded
    def backup(self, output):
        require(shutil.disk_usage(self.directory).free >= self.filename.stat().st_size+self.min_free,507,'Недостаточно места для резервной копии')
        connection = sqlite3.connect(output)
        try:
            self.db.backup(connection)
        finally:
            connection.close()
        Path(output).chmod(0o600)

    @guarded
    def close(self):
        self.db.close()
