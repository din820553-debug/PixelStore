"""WSGI application for one seller and token-protected customer orders."""
import base64
import csv
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
from http import HTTPStatus
from http.cookies import SimpleCookie
from threading import RLock
import time
from urllib.parse import parse_qs, quote, urlsplit
import uuid
import hmac

from .storage import Store, Fault, digest, require, ROOT

class Stream:
    def __init__(self, path, cleanup=False):
        self.path, self.cleanup = Path(path), cleanup
        self.file = self.path.open('rb')
    def __iter__(self):
        return self
    def __next__(self):
        block = self.file.read(65536)
        if not block:
            self.close()
            raise StopIteration
        return block
    def close(self):
        self.file.close()
        if self.cleanup:
            self.path.unlink(missing_ok=True)

class Response:
    def __init__(self, body=b'', status=200, mime='application/json; charset=utf-8', headers=None):
        self.body, self.status, self.mime = body, status, mime
        self.headers = list(headers or [])
        if isinstance(body, bytes) and not any(k.lower()=='content-length' for k,v in self.headers):
            self.headers.append(('Content-Length',str(len(body))))

class App:
    def __init__(self, data_dir=None, admin_token=None, public_origin=None, port=None, max_data_mb=None, min_free_mb=None):
        self.port = int(port or os.environ.get('SERVER_PORT') or os.environ.get('PORT') or 25011)
        self.origin = (public_origin or os.environ.get('PUBLIC_ORIGIN') or f'http://localhost:{self.port}').rstrip('/')
        parsed = urlsplit(self.origin)
        require(parsed.scheme in ('http','https') and parsed.hostname and parsed.path in ('','/') and not parsed.query and not parsed.fragment and parsed.username is None and parsed.password is None,500,'PUBLIC_ORIGIN должен содержать только origin сервера')
        require(parsed.scheme=='https' or parsed.hostname in ('localhost','127.0.0.1','::1'),500,'Для публичного адреса кабинета нужен HTTPS')
        self.secure = parsed.scheme=='https'
        self.public_ready = self.secure
        self.allowed = {self.origin, f'http://localhost:{self.port}', f'http://127.0.0.1:{self.port}'}
        self.allowed.update(x.strip() for x in os.environ.get('ALLOWED_ORIGINS','https://din820553-debug.github.io').split(',') if x.strip())
        require('*' not in self.allowed,500,'Укажите точные ALLOWED_ORIGINS, не *')
        self.admin_token = admin_token if admin_token is not None else os.environ.get('ADMIN_TOKEN','')
        require(not self.admin_token or len(self.admin_token)>=32,500,'ADMIN_TOKEN должен быть случайным и не короче 32 символов')
        self.store = Store(data_dir or os.environ.get('DATA_DIR') or ROOT/'data', max_data_mb=max_data_mb if max_data_mb is not None else os.environ.get('MAX_DATA_MB','300'), min_free_mb=min_free_mb if min_free_mb is not None else os.environ.get('MIN_FREE_MB','50'))
        with self.store.lock:
            self.store.db.execute('DELETE FROM sessions')
        self.rates, self.rate_lock = {}, RLock()

    def json_response(self, value, status=200, headers=None):
        return Response(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode(), status, headers=headers)

    def body(self, env, maximum=32000):
        try:
            length = int(env.get('CONTENT_LENGTH') or '0')
        except ValueError:
            raise Fault(400,'Неверный размер запроса')
        require(0<length<=maximum,413 if length>maximum else 400,'Слишком большой или пустой запрос')
        try:
            value = json.loads(env['wsgi.input'].read(length).decode())
        except (ValueError,UnicodeError):
            raise Fault(400,'Нужен корректный JSON')
        require(isinstance(value,dict),400,'Нужен объект JSON')
        return value

    def limit(self, env, group, maximum, period=60):
        key = (env.get('REMOTE_ADDR','unknown'),group)
        now = time.monotonic()
        with self.rate_lock:
            count, until = self.rates.get(key,(0,now+period))
            if until<=now:
                count, until = 0,now+period
            self.rates[key]=(count+1,until)
            if len(self.rates)>10000:
                self.rates={k:v for k,v in self.rates.items() if v[1]>now}
            require(count<maximum,429,'Слишком много запросов. Попробуйте позже')

    def cookie_token(self, env):
        cookie=SimpleCookie()
        try:
            cookie.load(env.get('HTTP_COOKIE',''))
            return cookie['ps_admin'].value if 'ps_admin' in cookie else ''
        except Exception:
            return ''

    def require_admin(self, env):
        token=self.cookie_token(env)
        with self.store.lock:
            row=self.store.db.execute('SELECT token_hash FROM sessions WHERE token_hash=? AND expires_at>?',(digest(token),int(time.time()*1000))).fetchone() if re.fullmatch('[a-f0-9]{64}',token) else None
        require(row is not None,401,'Войдите в кабинет продавца')
        require(not env.get('HTTP_ORIGIN') or env['HTTP_ORIGIN']==self.origin,403,'Откройте кабинет на адресе сервера')

    def customer_token(self, env):
        header=env.get('HTTP_AUTHORIZATION','')
        return header[7:] if header.startswith('Bearer ') else ''

    def download(self, doc):
        return Response(doc['data'],mime=doc['mime'],headers=[('Content-Security-Policy',"sandbox; default-src 'none'"),('Content-Disposition',"attachment; filename*=UTF-8''"+quote(doc['name'],safe=''))])

    def csv_response(self):
        stream=io.StringIO(newline='')
        writer=csv.writer(stream,delimiter=';',quoting=csv.QUOTE_ALL,lineterminator='\r\n')
        writer.writerow(['Номер','Создан','Никнейм','Режим','Оплата','Выполнение','Сумма ₽','Возврат ₽','Расходы ₽','Остаток ₽','Заметка'])
        for order in self.store.list():
            costs=sum(order['costs'].values())
            values=[order['number'],order['createdAt'],order['nickname'],order['mode'],order['paymentStatus'],order['fulfillmentStatus'],order['total'],order['refundedKopecks']/100,costs/100,(order['totalKopecks']-order['refundedKopecks']-costs)/100,order['note']]
            writer.writerow([("'"+str(v)) if re.match(r'^\s*[=+\-@]',str(v)) else str(v) for v in values])
        return Response(('\ufeff'+stream.getvalue()).encode(),mime='text/csv; charset=utf-8',headers=[('Content-Disposition','attachment; filename="pixelstore-orders.csv"')])

    def handle_api(self, env, route, method):
        origin=env.get('HTTP_ORIGIN')
        require(not origin or origin in self.allowed,403,'Этот сайт не разрешён настройкой сервера')
        if method=='OPTIONS':
            return Response(status=204,headers=[('Access-Control-Allow-Methods','GET,POST,PATCH,OPTIONS'),('Access-Control-Allow-Headers','Content-Type,Authorization,Idempotency-Key'),('Access-Control-Max-Age','600')])
        if method=='GET' and route=='/api/health':
            return self.json_response(dict(ok=True,storage='server',paymentsConnected=False,publicApiReady=self.public_ready))
        if method=='POST' and route=='/api/admin/login':
            require(not origin or origin==self.origin,403,'Вход доступен на настроенном HTTPS-адресе сервера')
            self.limit(env,'login',8,900)
            require(bool(self.admin_token),503,'Задайте ADMIN_TOKEN в окружении хостинга')
            token=self.body(env).get('token','')
            require(isinstance(token,str) and hmac.compare_digest(digest(token),digest(self.admin_token)),401,'Неверный ключ продавца')
            raw=secrets.token_hex(32)
            with self.store.lock:
                self.store.db.execute('DELETE FROM sessions WHERE expires_at<?',(int(time.time()*1000),))
                self.store.db.execute('INSERT INTO sessions VALUES(?,?)',(digest(raw),int(time.time()*1000)+12*3600000))
            cookie=f'ps_admin={raw}; HttpOnly; SameSite=Strict; Path=/api; Max-Age=43200'+('; Secure' if self.secure else '')
            return self.json_response(dict(ok=True),headers=[('Set-Cookie',cookie)])
        if route.startswith('/api/admin/'):
            self.require_admin(env)
            self.limit(env,'admin',300)
            if method=='GET' and route=='/api/admin/session':
                return self.json_response(dict(ok=True))
            if method=='POST' and route=='/api/admin/logout':
                with self.store.lock:
                    self.store.db.execute('DELETE FROM sessions WHERE token_hash=?',(digest(self.cookie_token(env)),))
                return self.json_response(dict(ok=True),headers=[('Set-Cookie','ps_admin=; HttpOnly; SameSite=Strict; Path=/api; Max-Age=0'+('; Secure' if self.secure else ''))])
            if route=='/api/admin/orders':
                if method=='GET':
                    query={k:v[-1] for k,v in parse_qs(env.get('QUERY_STRING','')).items()}
                    return self.json_response(dict(orders=self.store.list(query)))
                if method=='POST':
                    result=self.store.create(self.body(env),env.get('HTTP_IDEMPOTENCY_KEY'),True)
                    return self.json_response(result,200 if result['repeated'] else 201)
            if method=='GET' and route=='/api/admin/export.csv':
                return self.csv_response()
            if method=='GET' and route=='/api/admin/backup':
                output=self.store.directory/('backup-'+str(uuid.uuid4())+'.sqlite')
                try:
                    self.store.backup(output)
                    return Response(Stream(output,cleanup=True),mime='application/octet-stream',headers=[('Content-Length',str(output.stat().st_size)),('Content-Disposition','attachment; filename="pixelstore-private-backup.sqlite"')])
                except BaseException:
                    output.unlink(missing_ok=True)
                    raise
            if method=='GET' and route=='/api/admin/storage':
                with self.store.lock:
                    size=self.store.usage()
                return self.json_response(dict(usedBytes=size,limitBytes=self.store.max_bytes))
            match=re.fullmatch(r'/api/admin/orders/([a-f0-9-]{36})',route)
            if match:
                if method=='GET':
                    return self.json_response(dict(order=self.store.admin_order(match[1])))
                if method=='PATCH':
                    return self.json_response(dict(order=self.store.change(match[1],self.body(env))))
            match=re.fullmatch(r'/api/admin/orders/([a-f0-9-]{36})/documents',route)
            if match and method=='POST':
                body=self.body(env,7500000)
                encoded=body.get('base64')
                require(isinstance(encoded,str) and len(encoded)>0,400,'Неверный файл')
                try:
                    data=base64.b64decode(encoded,validate=True)
                except ValueError:
                    raise Fault(400,'Неверный файл')
                require(0<len(data)<=5*1024*1024,413,'Файл должен быть не больше 5 МБ')
                if data.startswith(b'%PDF-'):
                    mime='application/pdf'
                elif data.startswith(b'\x89PNG\r\n\x1a\n'):
                    mime='image/png'
                elif data.startswith(b'\xff\xd8\xff'):
                    mime='image/jpeg'
                elif data.startswith(b'RIFF') and data[8:12]==b'WEBP':
                    mime='image/webp'
                else:
                    raise Fault(400,'Разрешены PDF, JPEG, PNG и WebP')
                body['mime']=mime
                return self.json_response(dict(order=self.store.add_document(match[1],body,data)),201)
            match=re.fullmatch(r'/api/admin/documents/([a-f0-9-]{36})',route)
            if match:
                if method=='GET':
                    return self.download(self.store.document(match[1],True))
                if method=='PATCH':
                    return self.json_response(dict(order=self.store.share_document(match[1],self.body(env))))
        if route=='/api/orders' and method=='POST':
            self.limit(env,'create',15)
            result=self.store.create(self.body(env),env.get('HTTP_IDEMPOTENCY_KEY'))
            return self.json_response(result,200 if result['repeated'] else 201)
        match=re.fullmatch(r'/api/orders/([a-f0-9-]{36})',route)
        if match and method=='GET':
            self.limit(env,'read',120)
            return self.json_response(dict(order=self.store.customer(match[1],self.customer_token(env))))
        match=re.fullmatch(r'/api/documents/([a-f0-9-]{36})',route)
        if match and method=='GET':
            self.limit(env,'documents',60)
            return self.download(self.store.document(match[1],token=self.customer_token(env)))
        raise Fault(404,'Адрес API не найден')

    def handle_static(self, env, route, method):
        require(method in ('GET','HEAD'),405,'Метод недоступен')
        if route=='/api-config.js':
            # Without a public HTTPS domain, only local testing enables server checkout.
            js='window.PIXELSTORE_API_BASE='+('location.origin;' if self.public_ready else "(['localhost','127.0.0.1','::1'].includes(location.hostname)?location.origin:'');")
            return Response(js.encode(),mime='application/javascript; charset=utf-8')
        if route=='/catalog.js':
            from scripts.build_catalog import browser_catalog
            return Response(browser_catalog(self.store.catalog).encode(),mime='application/javascript; charset=utf-8')
        relative='index.html' if route=='/' else 'admin.html' if route in ('/admin','/admin/') else route.lstrip('/')
        target=(ROOT/relative).resolve()
        allowed={'index.html','style.css','app.js','orders.js','history.js','admin.html','admin.css','admin.js'}
        require(relative in allowed or (relative.startswith('assets/') and target.is_relative_to(ROOT/'assets')),404,'Страница не найдена')
        require(target.is_file(),404,'Страница не найдена')
        mime=mimetypes.guess_type(target)[0] or 'application/octet-stream'
        if mime.startswith('text/') or target.suffix=='.js':
            mime+='; charset=utf-8'
        return Response(b'' if method=='HEAD' else Stream(target),mime=mime,headers=[('Content-Length',str(target.stat().st_size))])

    def __call__(self, env, start_response):
        route=env.get('PATH_INFO','/')
        try:
            response=self.handle_api(env,route,env.get('REQUEST_METHOD','GET')) if route.startswith('/api/') else self.handle_static(env,route,env.get('REQUEST_METHOD','GET'))
        except Fault as error:
            response=self.json_response(dict(error=str(error)),error.status)
        except Exception as error:
            logging.error('Request failed: %s',type(error).__name__)
            response=self.json_response(dict(error='Ошибка сервера. Повторите запрос позже'),500)
        headers=[('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','no-referrer'),('Cache-Control','no-store'),('Content-Type',response.mime)]
        origin=env.get('HTTP_ORIGIN')
        if origin in self.allowed:
            headers.extend([('Access-Control-Allow-Origin',origin),('Vary','Origin'),('Access-Control-Allow-Credentials','true')])
        if not route.startswith('/api/'):
            headers.append(('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"))
        # Avoid a second Content-Length on static HEAD responses.
        unique={key.lower(): (key,value) for key,value in headers+response.headers}
        start_response(f'{response.status} {HTTPStatus(response.status).phrase}',list(unique.values()))
        return [response.body] if isinstance(response.body,bytes) else response.body

    def close(self):
        self.store.close()
