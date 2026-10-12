// CloudForge 控制台 · 后端行（AGH 插件）
//
// 面板不是独立工具：所有浏览器→daemon 的调用都经 ctx.services.register 声明，
// 由 AGH 的 BFF 转发、daemon 校验白名单与配额。AGH 的硬约束（决定了下面每个设计）：
//   · maxResultBytes ≤ 1,048,576 字节，timeoutMs ≤ 30,000（protocol/gen/ts/extension-service.ts:8）
//   · kind 只能是 'query' | 'effect'
//   · outputSchema 必填且每次调用都校验（ext-host/service-invocation.ts:169-170）
//   · 浏览器侧 services.call 还要过 agnes.client.json 的 services 白名单
// 点云/网格因此走 uint16 量化分页（见 lib/quantize.mjs），单批 65,536 点 ≈ 512 KB。

import { execFile } from 'node:child_process'
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { cloudMeta, cloudPage, meshPage } from './lib/quantize.mjs'
import { CloudFormatError } from './lib/cloud-io.mjs'
import { locateFreecad, stepToStl, readStlMeta } from './lib/freecad.mjs'
import { traceFromFile } from './lib/trace.mjs'
import { extractJson } from './lib/jsonout.mjs'

// ---------------------------------------------------------------- 工作区定位

const WORKSPACE = (() => {
  const candidates = [process.env.AGH_WORKSPACE, process.cwd(), dirname(process.cwd())].filter(Boolean)
  for (const c of candidates) {
    if (existsSync(join(c, 'hackathon', 'cloudforge-tools'))) return resolve(c)
  }
  return resolve(process.cwd())
})()

const TOOLS = join(WORKSPACE, 'hackathon', 'tools')
const DEMO = join(WORKSPACE, 'hackathon', 'demo-data')
const EVIDENCE = join(WORKSPACE, 'hackathon', 'evidence')
const TESTS = join(WORKSPACE, 'hackathon', 'tests', 'results')
const CLI =
  process.env.CLOUDFORGE_CLI || join(WORKSPACE, 'hackathon', 'cli', 'build', 'Release', 'cloudforge-cli.exe')
/** AGH 官方 CLI（用于 export 执行记录）。默认按本机同级仓库布局推断，可用 AGH_CLI 覆盖 */
const AGH_CLI_DEFAULT = join(dirname(WORKSPACE), 'agnes-harness', 'packages', 'cli', 'dist', 'local', 'agnes.mjs')

/** 扫描根：demo-data（任意深度）与 outputs（一级） */
const SCAN_ROOTS = () => [
  { dir: DEMO, depth: 4 },
  { dir: join(WORKSPACE, 'outputs'), depth: 2 },
]

// ---------------------------------------------------------------- 小工具

/** export job 状态（导出异步，服务 30s 超时上限不允许 await 整轮） */
const exportJobs = new Map()

const readJson = (path) => {
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return null
  }
}

const fmtKB = (n) => Math.max(1, Math.round(n / 1024))

function walkResultDirs(dir, depth, out) {
  if (depth < 0) return out
  let entries = []
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return out
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue
    const full = join(dir, e.name)
    if (e.name === 'node_modules' || e.name === 'build') continue
    if (existsSync(join(full, 'result.json'))) out.push(full)
    walkResultDirs(full, depth - 1, out)
  }
  return out
}

/** 真值文件约定：与 result.json 同级目录或上两级目录下的 *.truth.json（解析好绝对路径下发，
 *  别让客户端再自己拼——之前只给布尔值，导致客户端拼错路径、verify 永远跑不起来） */
function findTruthPath(resultDir) {
  const candidates = [
    join(resultDir, '..', 'demo.truth.json'),
    join(resultDir, '..', 'part.truth.json'),
    join(resultDir, '..', '..', 'demo.truth.json'),
    join(resultDir, '..', '..', 'part.truth.json'),
    join(resultDir, 'truth.json'),
  ]
  for (const c of candidates) {
    const p = resolve(c)
    if (existsSync(p)) return p
  }
  return null
}

/**
 * 解析该次检测对应的源点云路径。
 * 并非所有 result.json 都带 source_cloud（部分早期产物没有这个键），
 * 直接用 r.source_cloud 会让面板卡在"没有点云可看"，所以按可靠度逐级回退：
 *   1) result.json.source_cloud
 *   2) 同目录 info.json 的 file（cloudforge-cli info 的产物）
 *   3) run.meta.json 里记录的 cloud（由本面板的 cloudforge.run 写入）
 *   4) 同目录下唯一的 *.ply / *.pcd
 */
function resolveSourceCloud(resultDir, result) {
  if (result?.source_cloud && existsSync(result.source_cloud)) return resolve(result.source_cloud)
  const info = readJson(join(resultDir, 'info.json'))
  if (info?.file && existsSync(info.file)) return resolve(info.file)
  const meta = readJson(join(resultDir, 'run.meta.json'))
  if (meta?.cloud && existsSync(meta.cloud)) return resolve(meta.cloud)
  try {
    const clouds = readdirSync(resultDir, { withFileTypes: true })
      .filter((e) => e.isFile() && /\.(ply|pcd)$/i.test(e.name))
      .map((e) => join(resultDir, e.name))
    if (clouds.length === 1) return resolve(clouds[0])
  } catch {
    /* 目录不可读 */
  }
  return null
}

/** 该 run 目录附近能用的点云候选（供面板在 source_cloud 缺失时让用户挑） */
function candidateClouds(resultDir) {
  const dirs = [resultDir, join(resultDir, '..'), join(resultDir, '..', '..'), DEMO, join(WORKSPACE, 'outputs')]
  const seen = new Set()
  const out = []
  for (const d of dirs) {
    let entries = []
    try {
      entries = readdirSync(d, { withFileTypes: true })
    } catch {
      continue
    }
    for (const e of entries) {
      if (!e.isFile() || !/\.(ply|pcd)$/i.test(e.name)) continue
      const p = resolve(join(d, e.name))
      if (seen.has(p)) continue
      seen.add(p)
      let size = 0
      try {
        size = statSync(p).size
      } catch {
        continue
      }
      out.push({ path: p, name: e.name, dir: d, size })
      if (out.length >= 12) return out
    }
  }
  return out
}

/** 把 result.json + run.meta.json 合成一条工作流摘要 */
function workflowFrom(dir) {
  const rj = join(dir, 'result.json')
  const r = readJson(rj)
  if (!r || typeof r.shape_count !== 'number') return null
  const st = statSync(rj)
  const meta = readJson(join(dir, 'run.meta.json'))
  return {
    name: dir.split(/[\\/]/).pop(),
    dir,
    result: rj,
    shapes: r.shape_count,
    points: r.input_point_count ?? r.points ?? null,
    inliers: r.inlier_point_sum ?? null,
    remaining: r.remaining_count ?? null,
    // result.json 里没有 elapsed_ms（只在 CLI stdout），由 run.meta.json 补；两者都没有时如实为 null
    elapsedMs: meta?.elapsed_ms ?? null,
    preset: meta?.preset ?? null,
    at: meta?.at ?? null,
    coverage: r.input_point_count ? r.inlier_point_sum / r.input_point_count : null,
    updated: st.mtime.toISOString(),
    updatedMs: st.mtimeMs,
    hasIndices: existsSync(join(dir, 'indices')),
    sourceCloud: resolveSourceCloud(dir, r),
    truthPath: findTruthPath(dir),
  }
}

function collectWorkflows(limit = 8) {
  const out = []
  for (const { dir, depth } of SCAN_ROOTS()) {
    for (const d of walkResultDirs(dir, depth, [])) {
      const w = workflowFrom(d)
      if (w) out.push(w)
    }
  }
  return out.sort((a, b) => b.updatedMs - a.updatedMs).slice(0, limit)
}

const ARTIFACT_EXT = /\.(step|stp|stl|py|md)$/i
/** 注意：不能用 /\.st[ep]$/ —— 那样匹配不到 ".step"（.st 之后还有两个字符），STEP 会被误标成 report */
const artifactKind = (name) => {
  if (/\.(step|stp)$/i.test(name)) return 'step'
  if (/\.stl$/i.test(name)) return 'stl'
  if (/\.py$/i.test(name)) return 'script'
  return 'report'
}

/** 递归收集交付物（.step/.stp/.stl/.py/.md），depth 与工作流扫描一致 */
function collectArtifacts(limit = 20) {
  const out = []
  const walk = (dir, depth) => {
    if (depth < 0) return
    let entries = []
    try {
      entries = readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const e of entries) {
      const p = join(dir, e.name)
      if (e.isDirectory()) {
        if (e.name === 'node_modules' || e.name === 'build' || e.name === '.git') continue
        walk(p, depth - 1)
      } else if (e.isFile() && ARTIFACT_EXT.test(e.name)) {
        try {
          const st = statSync(p)
          if (st.size < 120 || st.size > 32 * 1024 * 1024) continue
          out.push({
            name: e.name,
            path: p,
            dir,
            kind: artifactKind(e.name),
            size: st.size,
            sizeKB: fmtKB(st.size),
            updated: st.mtime.toISOString(),
            updatedMs: st.mtimeMs,
          })
        } catch {
          /* 文件在扫描期间被删 */
        }
      }
    }
  }
  for (const { dir, depth } of SCAN_ROOTS()) walk(dir, depth)
  // STEP/STL 优先（CAD 页要用），同类型内按修改时间倒序
  const rank = (a) => (a.kind === 'step' || a.kind === 'stl' ? 0 : 1)
  return out.sort((a, b) => rank(a) - rank(b) || b.updatedMs - a.updatedMs).slice(0, limit)
}

function collectSuite() {
  const suite = readJson(join(TESTS, 'suite-result.json'))
  if (!suite) return null
  return {
    timestamp: suite.timestamp ?? null,
    total: suite.total ?? null,
    passed: suite.passed ?? null,
    failed: suite.failed ?? null,
    results: Array.isArray(suite.results) ? suite.results : [],
  }
}

function collectRobustness() {
  const rb = readJson(join(TESTS, 'robustness.json'))
  if (!rb) return null
  return {
    timestamp: rb.timestamp ?? null,
    description: rb.description ?? null,
    grid: Array.isArray(rb.grid) ? rb.grid : [],
    verdict: rb.verdict ?? null,
  }
}

function runProcess(exe, args, timeoutMs) {
  return new Promise((resolvePromise) => {
    execFile(exe, args, { timeout: timeoutMs, windowsHide: true, maxBuffer: 32 * 1024 * 1024 }, (err, stdout, stderr) => {
      resolvePromise({ err, stdout: String(stdout || ''), stderr: String(stderr || ''), code: err ? err.code ?? -1 : 0 })
    })
  })
}

const errText = (e) => String((e && (e.message || e)) || e).slice(0, 200)

// ---------------------------------------------------------------- 服务实现

const impl = {
  /** 总览：工作流 / 交付物 / 验证摘要 */
  report() {
    return {
      status: 'ready',
      workspace: WORKSPACE,
      cliAvailable: existsSync(CLI),
      cliPath: CLI,
      workflows: collectWorkflows(),
      artifacts: collectArtifacts(),
      verify: collectSuite(),
    }
  },

  /** 点云分页（uint16 量化） */
  cloud(input) {
    const file = String(input.cloud ?? '')
    const offset = Number(input.offset ?? 0)
    const limit = Number(input.limit ?? 65536)
    const page = cloudPage(file, offset, limit)
    return { ok: true, ...page }
  },

  /** 点云元信息（不传点，格式诊断用） */
  cloudMeta(input) {
    return { ok: true, ...cloudMeta(String(input.cloud ?? '')) }
  },

  /** 检测结果：result.json + indices 约定 */
  scene(input) {
    const dir = String(input.result_dir ?? '')
    const rj = join(dir, 'result.json')
    const r = readJson(rj)
    if (!r) return { ok: false, error: `读不到 result.json：${rj}` }
    const meta = readJson(join(dir, 'run.meta.json'))
    const shapes = (r.shapes ?? []).map((s, i) => ({
      id: s.id ?? i,
      type: s.type ?? 'Unknown',
      type_id: s.type_id ?? null,
      inlier_count: s.inlier_count ?? 0,
      rmse: s.rmse ?? null,
      robust_sigma: s.robust_sigma ?? null,
      soft_score: s.soft_score ?? null,
      coefficients: Array.isArray(s.coefficients) ? s.coefficients : [],
      indices: join(dir, 'indices', `shape_${s.id ?? i}.txt`),
    }))
    return {
      ok: true,
      result: rj,
      dir,
      source_cloud: r.source_cloud ?? null,
      // 前端真正用来载入点云的路径（已按回退链解析，见 resolveSourceCloud）
      sourceCloud: resolveSourceCloud(dir, r),
      candidateClouds: candidateClouds(dir),
      input_point_count: r.input_point_count ?? null,
      shape_count: r.shape_count ?? shapes.length,
      inlier_point_sum: r.inlier_point_sum ?? null,
      remaining_count: r.remaining_count ?? null,
      elapsed_ms: meta?.elapsed_ms ?? null,
      preset: meta?.preset ?? null,
      shapes,
    }
  },

  /** 网格分页：.stl 直读；.step/.stp 先经 freecadcmd 转 STL 并缓存 */
  async mesh(input) {
    const raw = String(input.path ?? '')
    if (!existsSync(raw)) return { ok: false, error: `文件不存在：${raw}` }
    const lower = raw.toLowerCase()
    let source = raw
    let converted = null
    if (/\.(step|stp)$/i.test(lower)) {
      converted = await stepToStl(WORKSPACE, raw, Number(input.deflection ?? 0.05))
      if (!converted.ok) return { ok: false, error: converted.error, source: 'step' }
      source = converted.stl
    } else if (!/\.stl$/i.test(lower)) {
      return { ok: false, error: `CAD 预览仅支持 .stl/.step/.stp，收到：${raw}` }
    }
    const offset = Number(input.offset ?? 0)
    const limit = Number(input.limit ?? 65536)
    const page = meshPage(source, offset, limit)
    const meta = readStlMeta(source)
    return {
      ok: true,
      ...page,
      source: /\.(step|stp)$/i.test(lower) ? 'step' : 'stl',
      originFile: raw,
      converted: converted
        ? {
            facets: converted.facets ?? null,
            cached: converted.cached ?? false,
            deflection: converted.deflection ?? null,
            // 网格与形状包围盒的相对偏差：0 表示预览完整覆盖了 STEP 实体。
            // 之前 MeshPart.meshFromShape 在复合体上退化（形状 Z 长 0.2056 m、网格只有 0.0106 m），
            // 这个数字就是用来把那种"缺一截却看不出来"的问题挡住的。
            coverage_error: converted.coverage_error ?? null,
          }
        : null,
      solid: meta
        ? {
            solids: meta.solids ?? null,
            volume_m3: meta.volume_m3 ?? null,
            bbox_m: meta.bbox_m ?? null,
            mesh_span_m: meta.mesh_span_m ?? null,
            facets: meta.facets ?? null,
            coverage_error: meta.coverage_error ?? null,
          }
        : null,
    }
  },

  /** verify：仓库没有落盘的 checks，需现跑一次（<1s） */
  async verify(input) {
    const result = String(input.result ?? '')
    const truth = String(input.truth ?? '')
    if (!existsSync(result)) return { ok: false, error: `result.json 不存在：${result}` }
    if (!existsSync(truth)) return { ok: false, error: `真值文件不存在：${truth}` }
    const args = ['verify', '--truth', truth, '--result', result]
    if (input.tolerance != null) args.push('--tol', String(input.tolerance))
    if (input.angle_tolerance_deg != null) args.push('--angle-deg', String(input.angle_tolerance_deg))
    const r = await runProcess(CLI, args, 60000)
    const parsed = extractJson(r.stdout)
    if (!parsed) return { ok: false, error: `verify 无有效输出：${errText(r.err ?? r.stderr)}` }
    return { ok: true, ...parsed, truth, result }
  },

  /** 套件 + 鲁棒性网格 */
  suite() {
    return { ok: true, suite: collectSuite(), robustness: collectRobustness() }
  },

  /** AGH 执行链（解析已导出的事件 JSONL） */
  trace(input) {
    const path = String(input.path ?? join(EVIDENCE, 'agh-trajectory-web.json'))
    const r = traceFromFile(path)
    if (!r.ok) return { ok: false, error: r.error, path }
    return {
      ok: true,
      path,
      eventCount: r.eventCount,
      firstTs: r.firstTs,
      lastTs: r.lastTs,
      model: r.model,
      turnEnd: r.turnEnd,
      calls: r.calls,
      verifies: r.verifies,
      stepCount: r.stepCount,
      chain: r.chain,
      compliance: r.compliance,
      report: typeof r.report === 'string' ? r.report.slice(0, 40000) : '',
    }
  },

  /** 调 cloudforge-cli 跑一个子命令（effect，需 AGH 授权） */
  async run(input) {
    if (!existsSync(CLI)) return { ok: false, error: `cloudforge-cli 未构建：${CLI}` }
    const op = String(input.op ?? '')
    const args = Array.isArray(input.args) ? input.args.map(String) : []
    const argv = [op, ...args]
    const t0 = Date.now()
    const r = await runProcess(CLI, argv, 300000)
    const parsed = extractJson(r.stdout)
    const wallMs = Date.now() - t0
    if (!parsed) return { ok: false, op, argv, wallMs, error: `无有效输出：${errText(r.err ?? r.stderr)}`, raw: r.stdout.slice(0, 400) }

    // result.json 不含 elapsed_ms/preset（只在 stdout），落一份 run.meta.json 供面板读取
    const outDir = input.output_dir ?? args.find((a) => args[a - 1] === '--output' || args[a - 1] === '--result') ?? null
    if (outDir && existsSync(outDir)) {
      try {
        writeFileSync(
          join(outDir, 'run.meta.json'),
          JSON.stringify(
            {
              op,
              argv,
              wall_ms: wallMs,
              elapsed_ms: parsed.elapsed_ms ?? null,
              preset: parsed.preset ?? null,
              shape_count: parsed.shape_count ?? null,
              input_point_count: parsed.input_point_count ?? parsed.points ?? null,
              at: new Date().toISOString(),
              cli: CLI,
            },
            null,
            1,
          ),
          'utf8',
        )
      } catch {
        /* 元信息写失败不影响主流程 */
      }
    }
    return { ok: parsed.ok !== false, op, argv, wallMs, ...parsed }
  },

  /** 拉起 FreeCAD / CloudForge GUI（effect） */
  async preview(input) {
    const target = input.target === 'cloudforge' ? 'launch-cloudforge.py' : 'preview-freecad.py'
    const script = join(TOOLS, target)
    if (!existsSync(script)) return { ok: false, error: `工具脚本缺失：${target}` }
    let exeArg = []
    if (target === 'preview-freecad.py') {
      const fc = await locateFreecad(WORKSPACE)
      if (!fc.ok || !fc.freecad_exe) return { ok: false, error: '未找到 FreeCAD' }
      exeArg = [fc.freecad_exe]
    }
    const r = await runProcess('python', [script, ...exeArg, String(input.file ?? '')], 90000)
    const parsed = extractJson(r.stdout)
    if (!parsed) return { ok: false, error: `预览脚本无输出：${errText(r.err ?? r.stderr)}` }
    return parsed
  },

  /**
   * 启动导出（effect）——调 AGH 官方 export 命令，不自研导出格式：
   *   agnes export <sessionId> [--raw] -o <file>
   * 产物与 hackathon/evidence/agh-trajectory-web.json 同格式（每行一个 EventEnvelope 的 JSONL）。
   *
   * 为什么"启动即返回"：AGH 服务 timeoutMs 上限 30_000，而整轮 export 可能更久；
   * 若在 handler 里 await，会被宿主按超时切断。这里 spawn 后立刻返回 job id，
   * 进度用 cloudforge.exportstatus（query）轮询。
   * --raw：参赛证据要保留 toolUseId / 审批策略等原始字段，脱敏后无法核查。
   */
  exportTrace(input) {
    const sessionId = String(input.session_id ?? '')
    if (!sessionId) return { ok: false, error: '缺少 session_id' }
    const agh = process.env.AGH_CLI || AGH_CLI_DEFAULT
    if (!existsSync(agh)) return { ok: false, error: `未找到 AGH CLI：${agh}` }
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
    const outJsonl = join(EVIDENCE, `session-${stamp}.jsonl`)
    const outHtml = join(EVIDENCE, `session-${stamp}.html`)
    const job = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
    const state = { job, sessionId, jsonl: outJsonl, html: outHtml, done: false, code: null, stderr: '', startedAt: Date.now() }
    exportJobs.set(job, state)

    const run = (args) =>
      new Promise((resolvePromise) => {
        execFile(process.execPath, args, { timeout: 300000, windowsHide: true, maxBuffer: 16 * 1024 * 1024 }, (err, stdout, stderr) => {
          resolvePromise({ err, stdout: String(stdout || ''), stderr: String(stderr || '') })
        })
      })

    void (async () => {
      const r1 = await run([agh, 'export', sessionId, '--raw', '-o', outJsonl])
      state.code = r1.err ? -1 : 0
      state.stderr = (r1.stderr || r1.err?.message || '').slice(0, 300)
      if (state.code === 0) {
        const r2 = await run([agh, 'export', sessionId, '--html', '--raw', '-o', outHtml])
        if (r2.err) state.htmlError = (r2.stderr || r2.err.message || '').slice(0, 200)
      }
      state.done = true
    })()

    return { ok: true, started: true, job, sessionId, jsonl: outJsonl, html: outHtml }
  },

  /** 导出进度（query） */
  exportStatus(input) {
    const job = String(input.job ?? '')
    const s = exportJobs.get(job)
    if (!s) return { ok: false, error: `未知 job：${job}` }
    const bytes = existsSync(s.jsonl) ? statSync(s.jsonl).size : 0
    return {
      ok: s.code === 0,
      done: s.done,
      code: s.code,
      job,
      jsonl: s.jsonl,
      html: existsSync(s.html) ? s.html : null,
      htmlError: s.htmlError ?? null,
      bytes,
      stderr: s.stderr,
      elapsedMs: Date.now() - s.startedAt,
    }
  },
}

// ---------------------------------------------------------------- 注册

/** 宽松输出 schema：字段做文档化声明，但允许额外字段，避免不同子命令的 JSON 差异导致校验失败 */
const out = (properties) => ({ type: 'object', properties, additionalProperties: true })

const SERVICES = [
  {
    name: 'cloudforge.report',
    kind: 'query',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    outputSchema: out({
      status: { type: 'string' },
      workspace: { type: 'string' },
      cliAvailable: { type: 'boolean' },
      workflows: { type: 'array' },
      artifacts: { type: 'array' },
      verify: { type: ['object', 'null'] },
    }),
    timeoutMs: 8000,
    maxResultBytes: 262144,
    handler: () => impl.report(),
  },
  {
    name: 'cloudforge.cloud',
    kind: 'query',
    inputSchema: {
      type: 'object',
      properties: { cloud: { type: 'string' }, offset: { type: 'number' }, limit: { type: 'number' } },
      required: ['cloud'],
      additionalProperties: false,
    },
    outputSchema: out({
      ok: { type: 'boolean' },
      file: { type: 'string' },
      format: { type: 'string' },
      total: { type: 'number' },
      offset: { type: 'number' },
      returned: { type: 'number' },
      bbox_min: { type: 'array' },
      bbox_max: { type: 'array' },
      span: { type: 'number' },
      encoding: { type: 'string' },
      origin: { type: 'array' },
      scale: { type: 'array' },
      positions: { type: 'string' },
    }),
    timeoutMs: 30000,
    maxResultBytes: 1048576,
    handler: (input) => guard(() => impl.cloud(input)),
  },
  {
    name: 'cloudforge.cloudmeta',
    kind: 'query',
    inputSchema: {
      type: 'object',
      properties: { cloud: { type: 'string' } },
      required: ['cloud'],
      additionalProperties: false,
    },
    outputSchema: out({
      ok: { type: 'boolean' },
      file: { type: 'string' },
      format: { type: 'string' },
      kind: { type: 'string' },
      points: { type: 'number' },
      bbox_min: { type: 'array' },
      bbox_max: { type: 'array' },
      span: { type: 'number' },
      centered: { type: 'boolean' },
    }),
    timeoutMs: 15000,
    maxResultBytes: 8192,
    handler: (input) => guard(() => impl.cloudMeta(input)),
  },
  {
    name: 'cloudforge.scene',
    kind: 'query',
    inputSchema: {
      type: 'object',
      properties: { result_dir: { type: 'string' } },
      required: ['result_dir'],
      additionalProperties: false,
    },
    outputSchema: out({
      ok: { type: 'boolean' },
      result: { type: 'string' },
      dir: { type: 'string' },
      source_cloud: { type: ['string', 'null'] },
      sourceCloud: { type: ['string', 'null'] },
      candidateClouds: { type: 'array' },
      input_point_count: { type: ['number', 'null'] },
      shape_count: { type: 'number' },
      inlier_point_sum: { type: ['number', 'null'] },
      remaining_count: { type: ['number', 'null'] },
      elapsed_ms: { type: ['number', 'null'] },
      preset: { type: ['string', 'null'] },
      shapes: { type: 'array' },
    }),
    timeoutMs: 8000,
    maxResultBytes: 524288,
    handler: (input) => guard(() => impl.scene(input)),
  },
  {
    name: 'cloudforge.mesh',
    kind: 'query',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string' },
        offset: { type: 'number' },
        limit: { type: 'number' },
        deflection: { type: 'number' },
      },
      required: ['path'],
      additionalProperties: false,
    },
    outputSchema: out({
      ok: { type: 'boolean' },
      file: { type: 'string' },
      format: { type: 'string' },
      source: { type: 'string' },
      originFile: { type: 'string' },
      total: { type: 'number' },
      offset: { type: 'number' },
      returned: { type: 'number' },
      bbox_min: { type: 'array' },
      bbox_max: { type: 'array' },
      encoding: { type: 'string' },
      origin: { type: 'array' },
      scale: { type: 'array' },
      positions: { type: 'string' },
      converted: { type: ['object', 'null'] },
      solid: { type: ['object', 'null'] },
    }),
    timeoutMs: 30000,
    maxResultBytes: 1048576,
    handler: (input) => guard(() => impl.mesh(input)),
  },
  {
    name: 'cloudforge.verify',
    kind: 'query',
    inputSchema: {
      type: 'object',
      properties: {
        result: { type: 'string' },
        truth: { type: 'string' },
        tolerance: { type: 'number' },
        angle_tolerance_deg: { type: 'number' },
      },
      required: ['result', 'truth'],
      additionalProperties: false,
    },
    outputSchema: out({
      ok: { type: 'boolean' },
      pass: { type: 'boolean' },
      matched: { type: 'number' },
      total: { type: 'number' },
      tolerance: { type: 'number' },
      angle_tolerance_deg: { type: 'number' },
      checks: { type: 'array' },
      truth: { type: 'string' },
      result: { type: 'string' },
    }),
    timeoutMs: 30000,
    maxResultBytes: 262144,
    handler: (input) => guard(() => impl.verify(input)),
  },
  {
    name: 'cloudforge.suite',
    kind: 'query',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    outputSchema: out({
      ok: { type: 'boolean' },
      suite: { type: ['object', 'null'] },
      robustness: { type: ['object', 'null'] },
    }),
    timeoutMs: 8000,
    maxResultBytes: 131072,
    handler: () => impl.suite(),
  },
  {
    name: 'cloudforge.trace',
    kind: 'query',
    inputSchema: {
      type: 'object',
      properties: { path: { type: 'string' } },
      additionalProperties: false,
    },
    outputSchema: out({
      ok: { type: 'boolean' },
      path: { type: 'string' },
      eventCount: { type: 'number' },
      firstTs: { type: ['string', 'null'] },
      lastTs: { type: ['string', 'null'] },
      model: { type: ['string', 'null'] },
      turnEnd: { type: ['object', 'null'] },
      calls: { type: 'array' },
      verifies: { type: 'array' },
      stepCount: { type: 'number' },
      chain: { type: 'array' },
      compliance: { type: 'object' },
      report: { type: 'string' },
    }),
    timeoutMs: 10000,
    maxResultBytes: 524288,
    handler: (input) => impl.trace(input),
  },
  {
    name: 'cloudforge.run',
    kind: 'effect',
    inputSchema: {
      type: 'object',
      properties: {
        op: { type: 'string' },
        args: { type: 'array' },
        output_dir: { type: 'string' },
      },
      required: ['op'],
      additionalProperties: false,
    },
    outputSchema: out({
      ok: { type: 'boolean' },
      op: { type: 'string' },
      argv: { type: 'array' },
      wallMs: { type: 'number' },
    }),
    timeoutMs: 30000,
    maxResultBytes: 262144,
    handler: (input) => guard(() => impl.run(input)),
  },
  {
    name: 'cloudforge.preview',
    kind: 'effect',
    inputSchema: {
      type: 'object',
      properties: { target: { type: 'string' }, file: { type: 'string' } },
      required: ['target', 'file'],
      additionalProperties: false,
    },
    outputSchema: out({ ok: { type: 'boolean' }, alive: { type: 'boolean' }, pid: { type: 'number' } }),
    timeoutMs: 30000,
    maxResultBytes: 16384,
    handler: (input) => guard(() => impl.preview(input)),
  },
  {
    name: 'cloudforge.export',
    kind: 'effect',
    inputSchema: {
      type: 'object',
      properties: { session_id: { type: 'string' } },
      required: ['session_id'],
      additionalProperties: false,
    },
    outputSchema: out({
      ok: { type: 'boolean' },
      started: { type: 'boolean' },
      job: { type: 'string' },
      sessionId: { type: 'string' },
      jsonl: { type: 'string' },
      html: { type: 'string' },
    }),
    timeoutMs: 30000,
    maxResultBytes: 16384,
    handler: (input) => guard(() => impl.exportTrace(input)),
  },
  {
    name: 'cloudforge.exportstatus',
    kind: 'query',
    inputSchema: {
      type: 'object',
      properties: { job: { type: 'string' } },
      required: ['job'],
      additionalProperties: false,
    },
    outputSchema: out({
      ok: { type: 'boolean' },
      done: { type: 'boolean' },
      code: { type: ['number', 'null'] },
      job: { type: 'string' },
      jsonl: { type: 'string' },
      html: { type: ['string', 'null'] },
      bytes: { type: 'number' },
      stderr: { type: 'string' },
      elapsedMs: { type: 'number' },
    }),
    timeoutMs: 8000,
    maxResultBytes: 16384,
    handler: (input) => impl.exportStatus(input),
  },
]

/** 把领域异常转成 {ok:false,error}，不要让 AGH 侧看到 INTERNAL_ERROR */
function guard(fn) {
  try {
    const r = fn()
    if (r && typeof r.then === 'function') return r.catch((e) => ({ ok: false, error: errText(e) }))
    return r
  } catch (e) {
    if (e instanceof CloudFormatError) return { ok: false, error: e.message }
    return { ok: false, error: errText(e) }
  }
}

export const runtime = {
  apply(ctx) {
    for (const spec of SERVICES) {
      const { handler, ...rest } = spec
      ctx.services.register({ ...rest, handler })
    }
  },
}

export { WORKSPACE, CLI, SERVICES }