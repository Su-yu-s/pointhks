// 补充验证：__CF_DEBUG 钩子 + 收起/展开交互是否真正生效
import { Window } from 'happy-dom'

const target = process.argv[2]
const win = new Window({ url: 'http://127.0.0.1:4177/' })
const { document } = win
globalThis.window = win
globalThis.document = document
globalThis.Node = win.Node
globalThis.Element = win.Element
globalThis.HTMLElement = win.HTMLElement

const out = { checks: {} }
const service = {
  call: async (name) =>
    name === 'cloudforge.report'
      ? { workflows: [{ name: 'wf', shapes: 1, points: 10, inliers: 9, elapsedMs: 1 }], artifacts: [], verify: null }
      : { ok: true, alive: true, pid: 1 },
}
const ctx = { agnes: { services: service, sessions: new Map() }, session: { sessionId: 's1' } }
const mod = await import('file:///' + target.replace(/\\/g, '/'))
mod.apply(ctx, {})
await new Promise((r) => setTimeout(r, 30))

const dbg = win.__CF_DEBUG
out.checks.debugHookExists = !!dbg
out.checks.hasTick = typeof dbg?.tick === 'function'
out.checks.hasRenderData = typeof dbg?.renderData === 'function'
out.checks.hasRoot = !!dbg?.root
out.checks.state = dbg?.state?.()

const root = document.getElementById('cf-console')
const toggle = document.querySelector('.cf-toggle')
const headMain = document.querySelector('.cf-head-main')
out.checks.beforeToggle = { width: root.style.width, headMainDisplay: headMain.style.display, toggleText: toggle.textContent }

toggle.dispatchEvent(new win.Event('click'))
out.checks.afterCollapse = {
  width: root.style.width,
  headMainDisplay: headMain.style.display,
  bodyDisplay: document.querySelector('.cf-body').style.display,
  toggleText: toggle.textContent,
}

toggle.dispatchEvent(new win.Event('click'))
out.checks.afterExpand = { width: root.style.width, headMainDisplay: headMain.style.display, toggleText: toggle.textContent }

// renderData 钩子：传 undefined 不应抛错（默认 {}），传数据应刷新
let hookErr = null
try {
  dbg.renderData(undefined)
  dbg.renderData({ workflows: [{ name: 'INJECTED', shapes: 9 }], artifacts: [], verify: null })
} catch (e) {
  hookErr = String(e.message || e)
}
out.checks.renderDataHookError = hookErr
out.checks.bodyHasInjected = (document.querySelector('.cf-body').textContent || '').includes('INJECTED')

console.log(JSON.stringify(out, null, 2))
process.exit(0)
