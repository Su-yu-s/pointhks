import { createRequire } from 'node:module';
import { hasPrivateDaclSync } from '../packages/system-node/src/index.ts';
const req = createRequire(import.meta.url);
const resolved = req.resolve('@agnes/system-node/native');
console.log('resolved path:', resolved);
const cand = req(resolved);
console.log('functions:', Object.keys(cand).filter(k => typeof cand[k] === 'function').length, '| abi:', cand.abiVersion);
console.log('TS wrapper works:', hasPrivateDaclSync('C:/Users/27285/.agh/secrets'));

