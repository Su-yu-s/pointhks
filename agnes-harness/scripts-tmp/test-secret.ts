import { hasPrivateDaclSync, windowsReadPrivateTextSync } from '../packages/system-node/src/index.ts';
const path = 'C:/Users/27285/.agh/secrets';
try {
  console.log('root dacl:', hasPrivateDaclSync(path));
  console.log('ns dacl:', hasPrivateDaclSync(path + '/agnes-ai'));
  const v = windowsReadPrivateTextSync(path + '/agnes-ai/account-7a6bb71059ea2ff815b2beed-r1', 1048576);
  console.log('read OK, length:', v.length);
} catch (e) {
  console.log('FAILED:', e.message, e.code || '');
}
