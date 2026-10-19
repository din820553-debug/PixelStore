import base64
import copy
import io
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
import uuid
from server.app import App
from server.storage import Store, Fault, ROOT
from scripts.build_catalog import browser_catalog

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.key=secrets.token_hex(32)
        self.app=App(data_dir=self.tmp.name,admin_token=self.key,public_origin='http://127.0.0.1:25011',min_free_mb=0)
        self.cookie=''
    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()
    def req(self,path,method='GET',body=None,headers=None):
        route,_,query=path.partition('?')
        data=json.dumps(body).encode() if body is not None else b''
        env={'PATH_INFO':route,'QUERY_STRING':query,'REQUEST_METHOD':method,'wsgi.input':io.BytesIO(data),'CONTENT_LENGTH':str(len(data)),'REMOTE_ADDR':'127.0.0.1','HTTP_COOKIE':self.cookie}
        for k,v in (headers or {}).items():
            env['HTTP_'+k.upper().replace('-','_')]=v
        result={}
        def start(status,h):
            result.update(status=int(status.split()[0]),headers=dict((k.lower(),v) for k,v in h))
        stream=self.app(env,start)
        try:
            result['data']=b''.join(stream)
        finally:
            if hasattr(stream,'close'):stream.close()
        if result['data'] and 'application/json' in result['headers']['content-type']:
            result['json']=json.loads(result['data'])
        return result
    def login(self):
        r=self.req('/api/admin/login','POST',{'token':self.key})
        self.assertEqual(r['status'],200,r)
        self.assertIn('HttpOnly',r['headers']['set-cookie'])
        self.assertIn('SameSite=Strict',r['headers']['set-cookie'])
        self.cookie=r['headers']['set-cookie'].split(';')[0]
    def make(self,**overrides):
        body=dict(nickname='Steve',items=[dict(id='vip',qty=1)],mvpPlusConfirmed=False,accessToken=secrets.token_hex(32),expectedTotalKopecks=73400)
        body.update(overrides)
        key=uuid.uuid4().hex
        return self.req('/api/orders','POST',body,{'Idempotency-Key':key}),body,key
    def test_price_confirmation_snapshot_idempotency(self):
        made,body,key=self.make(mode='manual',paymentStatus='paid',totalKopecks=1)
        self.assertEqual(made['status'],201,made)
        o=made['json']['order']
        self.assertEqual((o['totalKopecks'],o['mode'],o['paymentStatus']),(73400,'demo','unpaid'))
        self.app.store.catalog=copy.deepcopy(self.app.store.catalog)
        self.app.store.catalog[0]['usd']=99
        repeated=self.req('/api/orders','POST',body,{'Idempotency-Key':key})
        self.assertEqual(repeated['status'],200)
        self.assertEqual(repeated['json']['order']['id'],o['id'])
        self.assertEqual(repeated['json']['order']['totalKopecks'],73400)
        self.assertEqual(self.req('/api/orders','POST',body|{'nickname':'Alex'},{'Idempotency-Key':key})['status'],409)
        self.assertEqual(self.make(expectedTotalKopecks=1)[0]['status'],409)
        self.assertEqual(self.make(items=[dict(id='mvpplusplus',qty=1)],expectedTotalKopecks=83900)[0]['status'],400)
        self.assertEqual(self.make(items=[dict(id='mvpplusplus',qty=1)],expectedTotalKopecks=83900,mvpPlusConfirmed=True)[0]['status'],201)
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM orders').fetchone()[0],2)
    def test_auth_cors_private_static_and_https_config(self):
        made,body,_=self.make()
        oid=made['json']['order']['id']
        self.assertEqual(self.req('/api/orders/'+oid)['status'],404)
        self.assertEqual(self.req('/api/admin/orders')['status'],401)
        for path in ('/.env','/data/pixelstore.sqlite','/server/storage.py','/assets/../run.py','/data/settings.json','/catalog.json'):
            self.assertEqual(self.req(path)['status'],404,path)
        self.assertEqual(self.req('/api/health',headers={'Origin':'https://evil.example'})['status'],403)
        self.login()
        o=self.req('/api/orders/'+oid,headers={'Authorization':'Bearer '+body['accessToken']})['json']['order']
        self.assertNotIn('costs',o)
        self.assertNotIn('note',o)
        self.assertNotIn('events',o)
        self.assertEqual(self.req('/api/admin/orders/'+oid,'PATCH',{'version':1,'action':'note','note':'x'},{'Origin':'https://din820553-debug.github.io'})['status'],403)
        self.assertEqual(self.req('/api/orders','OPTIONS',headers={'Origin':'https://din820553-debug.github.io'})['headers']['access-control-allow-origin'],'https://din820553-debug.github.io')
        head=self.req('/style.css','HEAD')
        self.assertEqual(head['data'],b'')
        self.assertEqual(int(head['headers']['content-length']),(ROOT/'style.css').stat().st_size)
        self.assertIn(b'localhost',self.req('/api-config.js')['data'])
        with self.assertRaises(Fault):
            App(data_dir=self.tmp.name,public_origin='http://example.com')
    def test_manual_payment_fulfillment_expenses_audit_and_refund(self):
        self.login()
        made,_,_=self.make()
        oid=made['json']['order']['id']
        def patch(**body):
            o=self.app.store.admin_order(oid)
            return self.req('/api/admin/orders/'+oid,'PATCH',{'version':o['version']}|body)
        self.assertEqual(patch(action='fulfillment',status='processing')['status'],409)
        self.assertEqual(patch(action='payment',status='paid',reference='',amountKopecks=73400)['status'],400)
        self.assertEqual(patch(action='payment',status='paid',reference='bank-123',amountKopecks=73400)['status'],200)
        self.assertEqual(self.app.store.admin_order(oid)['fulfillmentStatus'],'new')
        self.assertEqual(self.req('/api/admin/orders/'+oid,'PATCH',dict(version=1,action='note',note='stale'))['status'],409)
        self.assertEqual(patch(action='fulfillment',status='processing')['status'],200)
        self.assertEqual(patch(action='fulfillment',status='completed')['status'],400)
        self.assertEqual(patch(action='fulfillment',status='completed',reference='supplier-123')['status'],200)
        costs=patch(action='costs',costs=dict(purchase=67020,cardFee=288,processingFee=2936))
        self.assertEqual(costs['json']['order']['costs']['cardFee'],288)
        self.assertIn('costs.changed',[e['type'] for e in costs['json']['order']['events']])
        patch(action='note',note='=HYPERLINK("bad")')
        self.assertIn(b"'=HYPERLINK",self.req('/api/admin/export.csv')['data'])
        refunded=patch(action='payment',status='refunded',reference='refund-123',amountKopecks=73400)['json']['order']
        self.assertEqual((refunded['refundedKopecks'],refunded['fulfillmentStatus']),(73400,'completed'))
    def test_documents_sharing_revocation_and_complete_backup(self):
        self.login()
        made,body,_=self.make()
        oid=made['json']['order']['id'];data=b'%PDF-1.4\nSynthetic test document'
        auth={'Authorization':'Bearer '+body['accessToken']}
        docs=[]
        for shared in (False,True):
            r=self.req('/api/admin/orders/'+oid+'/documents','POST',dict(kind='supplier',name='receipt.pdf',base64=base64.b64encode(data).decode(),shared=shared))
            self.assertEqual(r['status'],201,r)
            order=r['json']['order']
            docs.append(order['documents'][-1]['id'])
        self.assertEqual(self.req('/api/documents/'+docs[0],headers=auth)['status'],404)
        customer=self.req('/api/orders/'+oid,headers=auth)['json']['order']
        self.assertEqual(len(customer['documents']),1)
        self.assertEqual(self.req('/api/documents/'+docs[1],headers=auth)['data'],data)
        self.assertEqual(self.req('/api/documents/'+docs[1],headers={'Authorization':'Bearer '+secrets.token_hex(32)})['status'],404)
        revoke=self.req('/api/admin/documents/'+docs[1],'PATCH',dict(version=order['version'],shared=False))
        self.assertEqual(revoke['status'],200)
        self.assertEqual(self.req('/api/documents/'+docs[1],headers=auth)['status'],404)
        invalid=self.req('/api/admin/orders/'+oid+'/documents','POST',dict(kind='other',name='bad.html',base64=base64.b64encode(b'<script>bad</script>').decode()))
        self.assertEqual(invalid['status'],400)
        backup=self.req('/api/admin/backup')
        self.assertEqual(backup['status'],200)
        restore=Path(self.tmp.name)/'restored.sqlite';restore.write_bytes(backup['data'])
        with sqlite3.connect(restore) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM documents').fetchone()[0],2)
            self.assertEqual(conn.execute('SELECT data FROM documents LIMIT 1').fetchone()[0],data)
        self.assertFalse(list(Path(self.tmp.name).glob('backup-*.sqlite')))
    def test_reopen_and_session_invalidation(self):
        self.login();made,body,_=self.make();oid=made['json']['order']['id']
        other=App(data_dir=self.tmp.name,admin_token=self.key)
        try:
            self.assertEqual(other.store.customer(oid,body['accessToken'])['number'],made['json']['order']['number'])
            self.assertEqual(self.req('/api/admin/session')['status'],401)
        finally:other.close()
    def test_manual_order_and_disk_capacity(self):
        self.login()
        r=self.req('/api/admin/orders','POST',dict(nickname='Alex',items=[dict(id='mvpplusplus',qty=1)],mvpPlusConfirmed=True,accessToken=secrets.token_hex(32)),{'Idempotency-Key':uuid.uuid4().hex})
        self.assertEqual(r['status'],201,r)
        self.assertEqual((r['json']['order']['mode'],r['json']['order']['paymentStatus']),('manual','unpaid'))
        self.assertEqual(self.req('/api/admin/storage')['status'],200)
        self.app.store.max_bytes=1
        self.assertEqual(self.make()[0]['status'],507)
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM orders').fetchone()[0],1)
    def test_catalog_matches_static_frontend_and_historical_prices(self):
        self.assertEqual((ROOT/'catalog.js').read_text(),browser_catalog(json.loads((ROOT/'catalog.json').read_text())))
        expected={'vip':73400,'vipplus':157400,'mvp':314900,'mvpplus':472400,'mvpplusplus':83900}
        for product,price in expected.items():
            r,_,_=self.make(items=[dict(id=product,qty=1)],expectedTotalKopecks=price,mvpPlusConfirmed=True)
            self.assertEqual(r['status'],201,r)
            self.assertEqual(r['json']['order']['totalKopecks'],price)

if __name__=='__main__':unittest.main()
