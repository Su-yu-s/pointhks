// 探针：定位 daemon 启动时 “The credential store is unavailable.” 的确切原因
import { homedir } from 'node:os'
import { join } from 'node:path'
import { hasPrivateDaclSync, windowsPrivateFilesAvailable } from '../packages/system-node/src/index.ts'
import { credentialFileEnforcement, readCredentialFile } from '../packages/host/src/adapters/credential-files.ts'

const home = join(homedir(), '.agh')
const ref = 'secret://agnes-ai/account-7a6bb71059ea2ff815b2beed-r1'

console.log('windowsPrivateFilesAvailable =', windowsPrivateFilesAvailable())
const enf = credentialFileEnforcement({ os: 'win32' } as never)
console.log('enforcement =', JSON.stringify(enf))

for (const p of [
  home,
  join(home, 'secrets'),
  join(home, 'secrets', 'agnes-ai'),
  join(home, 'secrets', 'agnes-ai', 'account-7a6bb71059ea2ff815b2beed-r1'),
  join(home, 'auth'),
  join(home, 'locks'),
]) {
  let r: unknown
  try {
    r = hasPrivateDaclSync(p)
  } catch (e) {
    r = 'THROW ' + (e as Error).message
  }
  console.log('hasPrivateDacl', r === true ? 'PASS' : 'FAIL', p, '->', r)
}

try {
  const value = await readCredentialFile({ root: home, ref, kind: 'api-key', enforcement: enf })
  console.log('readCredentialFile OK, bytes =', value ? value.length : null)
} catch (e) {
  const err = e as { name?: string; code?: string; reason?: string; message?: string }
  console.log('readCredentialFile ERR', err.name, err.code, 'reason =', err.reason, '|', err.message)
}
