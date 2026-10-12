// 云铸面板 · 数据层
//
// AGH 没有自定义事件总线（v2.0 方案里的 DownstreamEvent/UpstreamEvent 一律不存在），真实通道只有三条：
//   面板 → 后端   ctx.agnes.services.call(name, input)   受 agnes.client.json 的 services 白名单约束
//   面板 → 智能体 ctx.session.commands.prompt / session.prompt
//   会话 → 面板   ctx.session.projection.subscribe + projection.read().opState（turn-end 自动刷新）
// effect（写操作）另走命令系统：registerEffect + commands.execute，内部是浏览器原生 confirm 授权。

import { decodePositions, decodeTriangles } from './decode.js'

export const PAGE = 65536

export function createData(ctx) {
  const service = ctx.agnes?.services
  const errors = []

  const call = async (name, input = {}) => {
    if (!service) throw new Error('无服务通道（工作台未注入 services）')
    try {
      return await service.call(name, input)
    } catch (e) {
      const msg = String((e && e.message) || e)
      errors.push({ name, msg })
      throw new Error(msg)
    }
  }

  const api = {
    errors,
    call,

    report: () => call('cloudforge.report', {}),
    scene: (resultDir) => call('cloudforge.scene', { result_dir: resultDir }),
    suite: () => call('cloudforge.suite', {}),
    trace: (path) => call('cloudforge.trace', path ? { path } : {}),
    cloudMeta: (cloud) => call('cloudforge.cloudmeta', { cloud }),
    verify: (result, truth, opts = {}) => call('cloudforge.verify', { result, truth, ...opts }),

    /** 单页点云载荷 */
    cloudPage: (cloud, offset, limit = PAGE) => call('cloudforge.cloud', { cloud, offset, limit }),

    /** 渐进加载：每页到货即回调，首屏 65,536 点（≈512 KB，服务端上限 1 MiB） */
    async loadCloud(cloud, { onPage, onProgress, signal } = {}) {
      const chunks = []
      let loadedPoints = 0
      let total = 0
      let first = null
      for (;;) {
        if (signal?.aborted) break
        const page = await api.cloudPage(cloud, loadedPoints, PAGE)
        if (page.ok === false) throw new Error(page.error || '点云读取失败')
        if (!first) {
          first = page
          total = page.total
        }
        const positions = decodePositions(page)
        if (positions && positions.length) {
          chunks.push(positions)
          loadedPoints += page.returned
        }
        onPage?.(positions, loadedPoints, page)
        onProgress?.({ loaded: loadedPoints, total, format: page.format, bbox: { min: page.bbox_min, max: page.bbox_max }, span: page.span })
        // 用「已载入点数」而不是块数来终止，否则会无限翻页
        if (loadedPoints >= page.total) break
        if (page.returned === 0) break
      }
      return {
        positions: concat(chunks),
        count: total,
        format: first?.format,
        bbox: first ? { min: first.bbox_min, max: first.bbox_max } : null,
        span: first?.span ?? 0,
      }
    },

    /** 网格（.stl 直读 / .step 经 freecadcmd 转换后） */
    async loadMesh(path, { onProgress } = {}) {
      const tris = []
      let loadedFaces = 0
      let first = null
      for (;;) {
        const page = await call('cloudforge.mesh', { path, offset: loadedFaces, limit: PAGE })
        if (page.ok === false) throw new Error(page.error || 'CAD 预览不可用')
        if (!first) first = page
        const decoded = decodeTriangles(page)
        if (decoded?.positions?.length) {
          tris.push(decoded.positions)
          loadedFaces += page.returned
        }
        onProgress?.({ loaded: loadedFaces, total: page.total })
        if (loadedFaces >= page.total || page.returned === 0) break
      }
      return {
        positions: concat(tris),
        triCount: first.total,
        source: first.source,
        originFile: first.originFile,
        converted: first.converted,
        solid: first.solid,
        bbox: { min: first.bbox_min, max: first.bbox_max },
      }
    },

    /** effect：面板自己调 CLI（会弹 AGH 的原生授权 confirm） */
    runEffect: (commandId, input) => ctx.commands.execute(commandId, input),

    /** 向当前会话发预置指令（反向驱动智能体） */
    async prompt(text) {
      const sid = ctx.session?.sessionId
      const sessions = ctx.agnes?.sessions
      const s = sid && sessions ? sessions.get(sid) : null
      if (!s || typeof s.prompt !== 'function') throw new Error('需要一个已打开的会话')
      await s.prompt(text)
      return true
    },

    /**
     * turn-end 自动刷新：opState 由非 null 变回 null 即为一轮结束。
     * 订阅只给"有变化"，所以每次都要重新 read() 判断当前态。
     */
    onTurnEnd(handler) {
      const proj = ctx.session?.projection
      if (!proj?.subscribe || !proj?.read) return () => {}
      let wasBusy = false
      const off = proj.subscribe(() => {
        void Promise.resolve(proj.read())
          .then((t) => {
            const busy = Boolean(t && t.opState)
            if (wasBusy && !busy) handler({ upto: t?.upto ?? null, generation: t?.generation ?? null })
            wasBusy = busy
          })
          .catch(() => {})
      })
      return off
    },

    /** 只读当前投影（执行链卡片用） */
    projection: () => Promise.resolve(ctx.session?.projection?.read?.()).catch(() => null),
  }

  return api
}

function concat(chunks) {
  if (chunks.length === 0) return new Float32Array(0)
  if (chunks.length === 1) return chunks[0]
  let total = 0
  for (const c of chunks) total += c.length
  const out = new Float32Array(total)
  let off = 0
  for (const c of chunks) {
    out.set(c, off)
    off += c.length
  }
  return out
}

