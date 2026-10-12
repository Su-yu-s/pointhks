// 用 AGH 官方管理 SDK 直调 daemon 卸载一个已装插件包：disable（如已启用）→ remove
// 运行：cd E:/C++/Point/agnes-harness && pnpm exec tsx scripts-tmp/remove-package.mts <packageId>

import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { createClient } from '../packages/sdk/src/index.node.ts'
import { memoryJournal } from '../packages/sdk/src/journal.ts'

const home = join(homedir(), '.agh')
const discovery = JSON.parse(readFileSync(join(home, 'data/daemon/discovery.json'), 'utf8'))

if (!discovery.ready) throw new Error('daemon not ready')
const profile = 'local-dev'

const client = createClient({
  transport: {
    kind: 'unix',
    path: discovery.socketPath,
    serverIdentity: { pid: discovery.owner.pid, processStartId: discovery.owner.processStartId },
  },
  auth: { kind: 'local' },
  journal: memoryJournal(randomUUID()),
})

const commandId = (kind: string) => `${kind}-${randomUUID().replaceAll('-', '')}`
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

async function waitOperation(opId: string, label: string) {
  for (let i = 0; i < 120; i++) {
    const op = await (client as any).packages.operation.get({ profile, operationId: opId })
    if (['completed', 'failed', 'cancelled', 'rolled-back'].includes(op.state)) {
      console.log(`[${label}] ${op.state} (${op.progress}%)`)
      if (op.state !== 'completed') {
        console.log(JSON.stringify(op, null, 2))
        throw new Error(`${label} failed`)
      }
      return op
    }
    await sleep(500)
  }
  throw new Error(`${label} timed out`)
}

const PACKAGE_ID = process.argv[2]
if (!PACKAGE_ID) throw new Error('usage: remove-package.mts <packageId>')
const clientId = await (client as any).clientId()

const existing = await (client as any).packages.list({ profile })
const target = existing.packages.find((p: any) => p.id === PACKAGE_ID)
if (!target) {
  console.log(`not installed: ${PACKAGE_ID}`)
  process.exit(0)
}
console.log(`found ${PACKAGE_ID}@${target.version} desired=${target.desired} trusted=${target.trusted}`)

if (target.desired === 'enabled') {
  const dis = await (client as any).packages.disable({
    profile,
    clientId,
    commandId: commandId('disable'),
    id: PACKAGE_ID,
  })
  await waitOperation(dis.operationId, 'disable')
}

const rm = await (client as any).packages.remove({
  profile,
  clientId,
  commandId: commandId('remove'),
  id: PACKAGE_ID,
})
await waitOperation(rm.operationId, 'remove')

const after = await (client as any).packages.list({ profile })
console.log('remaining:', after.packages.map((p: any) => `${p.id}@${p.version}(${p.desired})`).join(', '))
console.log(`DONE: ${PACKAGE_ID} removed`)
process.exit(0)
