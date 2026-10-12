// CloudForge 面板卡死复现/验证脚本（happy-dom 无头复现 AGH 客户端模块 apply 过程）
// 用法：node scripts-tmp/cf-panel-repro.mjs <绝对路径到 client/index.js>
// 复刻 packages/web-client/src/client-module.ts 注入的 ctx 形状：
//   ctx.agnes.services.call(name, input)  ← 只有 config.services 里声明过的服务可调
// 断言：apply() 是否抛错、body 是否仍为"连接中…"、setInterval 是否注册、service.call 是否被调

import { Window } from 'happy-dom'

const target = process.argv[2]
if (!target) {
  console.error('usage: node cf-panel-repro.mjs <path/to/client/index.js>')
  process.exit(2)
}
const fileUrl = 'file:///' + target.replace(/\\/g, '/').replace(/^\//, '/')

const win = new Window({ url: 'http://127.0.0.1:4177/' })
const { document } = win
globalThis.window = win
globalThis.document = document
globalThis.Node = win.Node
globalThis.Element = win.Element
globalThis.HTMLElement = win.HTMLElement

const out = {
  target,
  readyStateAtApply: document.readyState,
  applyError: null,
  domContentLoadedError: null,
  panelMounted: false,
  bodyHTMLAfterApply: null,
  bodyTextAfterApply: null,
  serviceBridgeSet: false,
  setIntervalCalls: [],
  serviceCalls: [],
  tickError: null,
  bodyTextAfterTick: null,
  sectionHeadersAfterTick: [],
  workflowRendered: false,
}

// 拦截 setInterval：记录 + 捕获 tick 函数，不真正排程
const realSetInterval = globalThis.setInterval
const tickFns = []
globalThis.setInterval = (fn, ms) => {
  tickFns.push(fn)
  out.setIntervalCalls.push(ms)
  return 0
}

// mock 服务：只放行声明过的服务，复刻 client-module 的门面语义
const declared = ['cloudforge.report', 'cloudforge.preview']
const service = {
  call: async (name, input) => {
    if (!declared.includes(name)) throw new Error('client service is unavailable: ' + name)
    out.serviceCalls.push(name)
    if (name === 'cloudforge.report')
      return {
        status: 'ready',
        workflows: [{ name: 'detect_livingroom', shapes: 6, points: 120000, inliers: 98000, elapsedMs: 421 }],
        artifacts: [{ name: 'livingroom.step', kind: 'step', size: 52310, path: 'E:/tmp/demo/livingroom.step' }],
        verify: { passed: 5, total: 5, date: '2026-10-07' },
      }
    return { ok: true, alive: true, pid: 1234 }
  },
}
const ctx = { agnes: { services: service, sessions: new Map() }, session: { sessionId: 'sess-1' } }

const mod = await import(fileUrl)
try {
  mod.apply(ctx, {})
} catch (e) {
  out.applyError = String((e && e.stack) || e).split('\n').slice(0, 3).join(' | ')
}

if (document.readyState === 'loading') {
  try {
    document.dispatchEvent(new win.Event('DOMContentLoaded'))
  } catch (e) {
    out.domContentLoadedError = String(e.message || e)
  }
}

await new Promise((r) => realSetInterval(r, 40))

const root = document.getElementById('cf-console')
const body = document.querySelector('.cf-body')
out.panelMounted = !!root
out.bodyHTMLAfterApply = body ? body.innerHTML.slice(0, 300) : null
out.bodyTextAfterApply = body ? (body.textContent || '').trim().slice(0, 120) : null
out.serviceBridgeSet = !!win.__CF_SERVICE

if (tickFns.length) {
  try {
    await tickFns[0]()
  } catch (e) {
    out.tickError = String(e.message || e)
  }
  const b2 = document.querySelector('.cf-body')
  out.bodyTextAfterTick = b2 ? (b2.textContent || '').trim().slice(0, 200) : null
  out.sectionHeadersAfterTick = [...document.querySelectorAll('.cf-body div')]
    .map((d) => (d.textContent || '').trim())
    .filter((t) => ['工作流', '验证', '交付物'].includes(t))
  out.workflowRendered = !!(out.bodyTextAfterTick || '').includes('detect_livingroom')
}

console.log(JSON.stringify(out, null, 2))
process.exit(0)
