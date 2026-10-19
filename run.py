"""Panel startup: PY_FILE=run.py, PY_PACKAGES=waitress==3.0.2."""
import os
import json
import tempfile
from pathlib import Path
from server.app import App

def main():
    try:
        from waitress import serve
    except ImportError:
        raise SystemExit('Укажите PY_PACKAGES=waitress==3.0.2 в панели или выполните pip install -r requirements.txt')
    os.umask(0o077)
    settings_path=Path(os.environ.get('DATA_DIR','data'))/'settings.json'
    settings={}
    if settings_path.exists():
        settings_path.chmod(0o600)
        settings=json.loads(settings_path.read_text(encoding='utf-8'))
        if not isinstance(settings,dict):
            raise SystemExit('data/settings.json должен содержать объект JSON')
    app=App(admin_token=os.environ.get('ADMIN_TOKEN') or settings.get('admin_token'),
            public_origin=os.environ.get('PUBLIC_ORIGIN') or settings.get('public_origin'),
            port=os.environ.get('SERVER_PORT') or os.environ.get('PORT') or settings.get('port'),
            max_data_mb=os.environ.get('MAX_DATA_MB') or settings.get('max_data_mb'),
            min_free_mb=os.environ.get('MIN_FREE_MB') or settings.get('min_free_mb'))
    temporary=app.store.directory/'tmp'
    temporary.mkdir(mode=0o700,exist_ok=True)
    tempfile.tempdir=str(temporary)
    host=os.environ.get('HOST','0.0.0.0')
    print(f'PixelStore Python: {host}:{app.port}. Один кабинет продавца. Платежи не подключены.',flush=True)
    if not app.public_ready:
        print('Публичный HTTPS-адрес не настроен: кабинет и серверное оформление доступны только локально. Не вводите секреты на публичном HTTP-адресе.',flush=True)
    try:
        serve(app,host=host,port=app.port,threads=2,connection_limit=64,channel_timeout=15,max_request_body_size=7500000,max_request_header_size=16384,inbuf_overflow=131072,outbuf_overflow=262144,outbuf_high_watermark=1048576,expose_tracebacks=False)
    finally:
        app.close()

if __name__=='__main__':
    main()
