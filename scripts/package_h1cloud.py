"""Build an upload archive from an explicit allowlist; never include runtime data."""
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
FILES=('run.py','requirements.txt','settings.example.json','.env.example','README.md','DESIGN.md',
       'index.html','style.css','app.js','orders.js','history.js','admin.html','admin.css','admin.js',
       'catalog.json','catalog.js','api-config.js','package.json',
       'server/__init__.py','server/app.py','server/storage.py','server/backup.py','server/schema.sql',
       'scripts/build_catalog.py','scripts/package_h1cloud.py','docs/STORAGE.md','test/test_storage.py')

def build(output):
    output=Path(output).resolve()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name in FILES:
            archive.write(ROOT/name,name)
        for item in sorted((ROOT/'assets').rglob('*')):
            if item.is_file() and item.suffix in ('.webp','.png','.jpg','.svg','.ico'):
                archive.write(item,item.relative_to(ROOT))
    return output

if __name__=='__main__':
    output=build(sys.argv[1] if len(sys.argv)>1 else ROOT.parent/'PixelStore-h1cloud-python.zip')
    print(f'Archive: {output} ({output.stat().st_size} bytes)')
