// 探针：直接问 daemon 的 _agnes/v1/clientModules.read，绕过 launcher 里吞异常的 try/catch，
// 让单个资源的真实失败原因暴露出来（404 在 web-server 里被统一成同一个空 body）。
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { createClient } from '../packages/sdk/src/index.node.ts'
import { memoryJournal } from '../packages/sdk/src/journal.ts'

const discovery = JSON.parse(readFileSync(`${homedir()}/.agh/data/daemon/discovery.json`, 'utf8'))
const client = createClient({
  transport: {
    kind: 'unix',
    path: discovery.socketPath,
    serverIdentity: { pid: discovery.owner.pid, processStartId: discovery.owner.processStartId },
  },
  auth: { kind: 'local' },
  journal: memoryJournal(`cf-asset-${Date.now()}`),
})

const revision = process.argv[2]
const paths = process.argv.slice(3)
for (const p of paths) {
  const full = `/plugins/cloudforge-panel/${revision}${p}`
  const started = Date.now()
  try {
    const r = await client.clientModules.read('local-dev', full)
    console.log(`${r.found ? 'FOUND ' : 'MISS  '} ${String(r.base64?.length ?? 0).padStart(9)} b64  ${Date.now() - started}ms  ${p}`)
  } catch (e) {
    const err = e as { code?: unknown; message?: unknown; data?: unknown }
    console.log(`THROW  ${p}`)
    console.log(`       code=${String(err.code)} message=${String(err.message)}`)
    console.log(`       data=${JSON.stringify(err.data)}`)
  }
}
process.exit(0)