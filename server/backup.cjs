'use strict';
const path=require('node:path');const fs=require('node:fs');const {openStore}=require('./store.cjs');
const directory=path.resolve(process.env.DATA_DIR||'data');const outDir=path.resolve(process.env.BACKUP_DIR||'backups');fs.mkdirSync(outDir,{recursive:true,mode:0o700});
const output=path.join(outDir,'pixelstore-'+new Date().toISOString().replaceAll(':','-')+'.sqlite');const store=openStore(directory);try{store.backup(output);console.log('Backup created:',output)}finally{store.close()}
