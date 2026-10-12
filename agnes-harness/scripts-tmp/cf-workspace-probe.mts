// 璇婃柇锛歞aemon 鐜板湪璁ゅ摢浜涘伐浣滃尯 / profile 瑙ｆ瀽鎴愪粈涔?/ 鏈夋病鏈夋ā鍨嬨€?// 鍙锛屼笉鏀逛换浣曠姸鎬併€傜敤娉曪細cd E:/C++/Point/agnes-harness && pnpm exec tsx scripts-tmp/cf-workspace-probe.mts
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
  journal: memoryJournal(`cf-ws-${Date.now()}`),
})

const show = (label, fn) =>
  Promise.resolve()
    .then(fn)
    .then((v) => console.log(`${label}: ${JSON.stringify(v)?.slice(0, 700)}`))
    .catch((e) => console.log(`${label}: ERROR ${String(e.code ?? '') + ' ' + String(e.message || e).slice(0, 200)}`))

await show('config.get', () => client.call('_agnes/v1/config.get', {}))
await show('workspace.list', () => client.call('_agnes/v1/workspace.list', {}))
await show('sessions.list', () => client.call('_agnes/v1/sessions.list', {}))
await show('providers/models', () => client.call('_agnes/v1/config.providers', {}))
process.exit(0)
