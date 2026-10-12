// 用 AGH 官方管理 SDK 直调 daemon 完成云铸插件包的安装流程：
// inspect → install → trust → enable（CLI 交互确认要求 TTY，SDK 是官方程序化管理路径）
// 运行：cd E:/C++/Point/agnes-harness && pnpm exec tsx scripts-tmp/install-package.mts

import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { createClient } from '../packages/sdk/src/index.node.ts'
import { memoryJournal } from '../packages/sdk/src/journal.ts'

const home = join(homedir(), '.agh')
const discovery = JSON.parse(readFileSync(join(home, 'data/daemon/discovery.json'), 'utf8'))
const owner = JSON.parse(readFileSync(join(home, 'data/daemon/owner.json'), 'utf8'))

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

const SOURCE = { type: 'file', ref: process.argv[2] ?? 'file:./hackathon/cloudforge-tools' }
const PACKAGE_ID = process.argv[3] ?? 'cloudforge-tools'
const clientId = await (client as any).clientId()
console.log(`daemon: ${discovery.socketPath}\nclientId: ${clientId}\nsource: ${SOURCE}`)

// 0) 幂等：已装旧版则先移除（否则 install 新版本会被包状态机拒绝）
const existing = await (client as any).packages.list({ profile })
const old = existing.packages.find((p: any) => p.id === PACKAGE_ID)
if (old) {
  console.log(`removing existing ${PACKAGE_ID}@${old.version} ...`)
  if (old.desired === 'enabled') {
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
}

// 1) inspect（receipt → 等 operation → preview）
const inspectReceipt = await (client as any).packages.inspect({
  profile,
  clientId,
  commandId: commandId('inspect'),
  source: SOURCE,
})
const inspectOp = await waitOperation(inspectReceipt.operationId, 'inspect')
const preview = inspectOp.preview
if (!preview) throw new Error('inspection completed without a preview')
console.log(`\npreview: ${preview.id}@${preview.version}`)
console.log(`integrity: ${preview.integrity}`)

// 2) install（保持 untrusted/disabled 默认）
const installReceipt = await (client as any).packages.install({
  profile,
  clientId,
  commandId: commandId('install'),
  source: preview.source ?? SOURCE,
  expectedIntegrity: preview.integrity,
})
const installedOp = await waitOperation(installReceipt.operationId, 'install')
const installed = installedOp.installed
if (!installed) throw new Error('no installed record')
console.log(`installed: ${installed.id}@${installed.version} trusted=${installed.trusted} desired=${installed.desired}`)
console.log(`installed keys: ${Object.keys(installed).join(', ')}`)

// 3) trust（绑定安装时输出的完整性 + 能力哈希）
const capabilityHash = installed.capabilityHash
if (!capabilityHash) throw new Error(`missing capabilityHash in installed record`)
const trustReceipt = await (client as any).packages.trust({
  profile,
  clientId,
  commandId: commandId('trust'),
  id: installed.id,
  expectedIntegrity: preview.integrity,
  capabilityHash,
})
await waitOperation(trustReceipt.operationId, 'trust')

// 4) enable
const enableReceipt = await (client as any).packages.enable({
  profile,
  clientId,
  commandId: commandId('enable'),
  id: installed.id,
})
await waitOperation(enableReceipt.operationId, 'enable')

const list = await (client as any).packages.list({ profile })
console.log('\npackages:', JSON.stringify(list.packages, null, 2))
console.log('\nDONE: package installed, trusted and enabled')
process.exit(0)
