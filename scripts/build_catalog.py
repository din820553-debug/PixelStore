"""Generate the static browser catalog from the server's JSON source."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def browser_catalog(products):
    data = json.dumps(products, ensure_ascii=False, separators=(',', ':'))
    return '(function(){const products=' + data + ';\nif(typeof module!=="undefined"&&module.exports)module.exports=products;else window.PIXELSTORE_CATALOG=products;})();\n'

if __name__ == '__main__':
    products = json.loads((ROOT / 'catalog.json').read_text())
    (ROOT / 'catalog.js').write_text(browser_catalog(products))
    print('catalog.js generated from catalog.json')
