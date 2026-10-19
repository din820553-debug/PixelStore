"""Create a private consistent SQLite copy including documents."""
from datetime import datetime, timezone
import os
import uuid
from pathlib import Path
from .storage import Store, ROOT

def main():
    target=Path(os.environ.get('BACKUP_DIR',ROOT/'backups'))
    target.mkdir(parents=True,mode=0o700,exist_ok=True)
    output=target/('pixelstore-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.sqlite')
    store=Store(os.environ.get('DATA_DIR',ROOT/'data'))
    try:
        store.backup(output)
        print('Backup created:',output)
    finally:
        store.close()

if __name__=='__main__':
    main()
