// 云铸面板 · FreeCAD 侧：定位内核、STEP→STL 转换（带缓存）、体积/包围盒复算
//
// 依赖本机 FreeCAD（现场演示机已装）。找不到时不抛错，而是返回 ok:false 让面板降级
// 为"只给文件卡 + FreeCAD 打开按钮"——CAD 预览不是主链路的必经环节。
//
// 转换结果缓存在 STEP 同目录的 preview.stl（mtime 变则失效），二次进入 CAD 页秒开。

import { execFile } from 'node:child_process'
import { existsSync, statSync, writeFileSync, readFileSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { extractJson } from './jsonout.mjs'

const PY_CANDIDATES = ['python', 'py', 'python3']

let freecadCache = null // { at, data }

function runProcess(exe, args, timeoutMs) {
  return new Promise((resolve) => {
    execFile(exe, args, { timeout: timeoutMs, windowsHide: true, maxBuffer: 8 * 1024 * 1024 }, (err, stdout, stderr) => {
      resolve({ err, stdout: String(stdout || ''), stderr: String(stderr || ''), code: err ? err.code ?? -1 : 0 })
    })
  })
}

async function firstWorkingPython() {
  for (const exe of PY_CANDIDATES) {
    const args = exe === 'py' ? ['-3', '--version'] : ['--version']
    const r = await runProcess(exe, args, 15000)
    if (!r.err) return exe
  }
  return null
}

/** 定位 FreeCAD（沿用 hackathon/tools/find-freecad.py 的逻辑，走 python） */
export async function locateFreecad(root) {
  if (freecadCache && Date.now() - freecadCache.at < 60000) return freecadCache.data
  const python = await firstWorkingPython()
  if (!python) {
    freecadCache = { at: Date.now(), data: { ok: false, error: 'python 不可用' } }
    return freecadCache.data
  }
  const script = join(root, 'hackathon', 'tools', 'find-freecad.py')
  if (!existsSync(script)) {
    freecadCache = { at: Date.now(), data: { ok: false, error: 'find-freecad.py 缺失' } }
    return freecadCache.data
  }
  const args = python === 'py' ? ['-3', script] : [script]
  const r = await runProcess(python, args, 30000)
  const data = extractJson(r.stdout) ?? { ok: false, error: 'FreeCAD 探测无输出' }
  freecadCache = { at: Date.now(), data }
  return data
}

/**
 * STEP → STL（带缓存）。成功返回 { ok, stl, facets, volume_m3, bbox_m, cached }
 * 失败返回 { ok:false, error }，由调用方决定降级。
 */
export async function stepToStl(root, stepPath, deflection = 0.05) {
  if (!existsSync(stepPath)) return { ok: false, error: `STEP 不存在：${stepPath}` }
  const stlPath = join(dirname(stepPath), 'preview.stl')
  const stepStat = statSync(stepPath)
  if (existsSync(stlPath)) {
    const stlStat = statSync(stlPath)
    if (stlStat.mtimeMs >= stepStat.mtimeMs && stlStat.size > 84) {
      // 缓存命中：sidecar 里存着上次转换的体积/包围盒/覆盖误差，直接复用（否则面板会显示 "—"）
      const side = readStlMeta(stlPath)
      return {
        ok: true,
        stl: stlPath,
        cached: true,
        facets: side?.facets ?? Math.floor((stlStat.size - 84) / 50),
        stl_bytes: stlStat.size,
        solids: side?.solids ?? null,
        volume_m3: side?.volume_m3 ?? null,
        bbox_m: side?.bbox_m ?? null,
        mesh_span_m: side?.mesh_span_m ?? null,
        coverage_error: side?.coverage_error ?? null,
        deflection: side?.deflection ?? null,
      }
    }
  }

  const fc = await locateFreecad(root)
  if (!fc.ok || !fc.freecadcmd_exe) return { ok: false, error: '未找到 FreeCAD（freecadcmd.exe），CAD 预览降级' }
  const script = join(root, 'hackathon', 'tools', 'step-to-stl.py')
  if (!existsSync(script)) return { ok: false, error: 'step-to-stl.py 缺失' }

  const r = await runProcess(fc.freecadcmd_exe, [script, stepPath, stlPath, String(deflection)], 120000)
  // freecadcmd 会在 JSON 之后追加大量进度条输出，必须做括号配平提取而非 indexOf+parse
  const parsed = extractJson(r.stdout)
  if (!parsed) return { ok: false, error: `转换无输出：${(r.stderr || r.err?.message || '').slice(0, 160)}` }
  if (!parsed.ok) return { ok: false, error: String(parsed.error || '转换失败').slice(0, 200) }
  if (!existsSync(stlPath)) return { ok: false, error: '转换未产出 STL' }
  // freecadcmd 有时在 stdout 混入诊断行，回写一份干净的 sidecar 供面板读取
  const metaPath = stlPath + '.json'
  try {
    writeFileSync(metaPath, JSON.stringify(parsed, null, 1), 'utf8')
  } catch {
    /* sidecar 只是加速手段，失败不影响预览 */
  }
  return { ...parsed, cached: false }
}

export function readStlMeta(stlPath) {
  const metaPath = stlPath + '.json'
  if (!existsSync(metaPath)) return null
  try {
    return JSON.parse(readFileSync(metaPath, 'utf8'))
  } catch {
    return null
  }
}

export { basename }