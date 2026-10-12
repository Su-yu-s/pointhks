// 探针：直接问 daemon 名册（_agnes/v1/clientModules.list），看 cloudforge-panel 是否在浏览器可见的名册里。
// 用法：cd E:/C++/Point/agnes-harness && pnpm exec tsx scripts-tmp/cf-roster-probe.mts
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { createClient } from '../packages/sdk/src/index.node.ts'
import { memoryJournal } from '../packages/sdk/src/journal.ts'

const home = join0(homedir())
function join0(p: string): string {
  return p
}

const discovery = JSON.parse(readFileSync(`${homedir()}/.agh/data/daemon/discovery.json`, 'utf8'))
const client = createClient({
  transport: {
    kind: 'unix',
    path: discovery.socketPath,
    serverIdentity: { pid: discovery.owner.pid, processStartId: discovery.owner.processStartId },
  },
  auth: { kind: 'local' },
  journal: memoryJournal(`cf-roster-${Date.now()}`),
})

const roster = await client.clientModules.list('local-dev')
console.log('modules:', roster.modules.length)
for (const m of roster.modules) {
  console.log('-', m.packageId)
  console.log('    rowId     =', m.rowId)
  console.log('    revision  =', String(m.revision))
  console.log('    entryUrl  =', m.entryUrl ?? '(none)')
  console.log('    slots     =', JSON.stringify(m.slots ?? []))
  console.log('    services  =', JSON.stringify(m.services ?? []))
  console.log('    catalogVer=', m.slotCatalogVersion ?? '(none)')
}
process.exit(0)