// cloudforge-tools — 把点云几何核心接入 Agnes Harness 的后端插件
//
// 五个工具全部经 cloudforge-cli.exe 子进程调用（C++ RANSAC 核心，静态链接）：
//   cloudforge_cloud_info  点云元信息（点数/包围盒/格式诊断）
//   cloudforge_detect      RANSAC 基元检测（含精拟合指标 RMSE/噪声σ/软评分）
//   cloudforge_refine      对点子集精拟合（<50 点会被核心拒绝）
//   cloudforge_synth       生成已知真值的合成点云（验证基准）
//   cloudforge_verify      检测结果 vs 真值的误差判定（支持圆柱↔圆锥泛化匹配）
//
// 零依赖插件：参数 Schema 手写 TypeBox kind 标记，CLI 位置可用环境变量 CLOUDFORGE_CLI 覆盖。
// CLI 输出为 JSON（前缀可能含 Schnabel 诊断行），解析时取第一个 '{' 起始的子串。

import { execFile } from 'node:child_process'
import { existsSync } from 'node:fs'
import { promisify } from 'node:util'

const run = promisify(execFile)
const Kind = Symbol.for('TypeBox.Kind')

const DEFAULT_CLI = 'E:/C++/Point/CloudForge/hackathon/cli/build/Release/cloudforge-cli.exe'

function cliPath() {
  return process.env.CLOUDFORGE_CLI || DEFAULT_CLI
}

async function runCli(args, { timeoutMs = 300000 } = {}) {
  const exe = cliPath()
  if (!existsSync(exe)) {
    return { ok: false, error: `cloudforge-cli not found: ${exe} (build hackathon/cli first, or set CLOUDFORGE_CLI)` }
  }
  try {
    const { stdout } = await run(exe, args, { timeout: timeoutMs, maxBuffer: 16 * 1024 * 1024, windowsHide: true })
    const start = stdout.indexOf('{')
    if (start < 0) return { ok: false, error: 'no JSON in cloudforge-cli output', raw: stdout.slice(0, 2000) }
    return JSON.parse(stdout.slice(start))
  } catch (err) {
    // 非零退出码：CLI 仍输出 JSON（ok:false 的业务失败，交回模型处理——这是闭环的一部分）
    const stdout = err && err.stdout ? err.stdout : ''
    const start = stdout.indexOf('{')
    if (start >= 0) {
      try {
        return JSON.parse(stdout.slice(start))
      } catch {
        /* 落入下方统一错误 */
      }
    }
    return { ok: false, error: `cloudforge-cli failed: ${err && err.message ? err.message : String(err)}` }
  }
}

// —— meta：8 个键全必填（AGH exactOptionalPropertyTypes 约定）——
const readOnlyMeta = {
  isReadOnly: true,
  isDestructive: false,
  isConcurrencySafe: true,
  isOpenWorld: false,
  replay: 'safe',
  costHint: undefined,
  deferLoading: false,
  requiresApproval: 'never',
}

// 写文件/耗时工具：显式要求审批（轨迹里留下审批记录，正是比赛治理叙事的证据）
const writeMeta = (wallMs) => ({
  isReadOnly: false,
  isDestructive: false,
  isConcurrencySafe: false,
  isOpenWorld: false,
  replay: 'never',
  costHint: { wallMs },
  deferLoading: false,
  requiresApproval: 'always',
})

const strParam = (desc, minLength = 1) => ({ [Kind]: 'String', type: 'string', minLength, description: desc })
const numParam = (desc) => ({ [Kind]: 'Number', type: 'number', description: desc })

function objSchema(properties, required) {
  return { [Kind]: 'Object', type: 'object', properties, required, additionalProperties: false }
}

export const cloudforgeTools = {
  inject: ['extension'],
  apply(ctx) {
    const agnes = ctx.extension()

    agnes.registerTool({
      name: 'cloudforge_cloud_info',
      description:
        'Inspect a point cloud file (PLY/PCD/TXT/STL/OBJ/NuScenes BIN): returns point count, bounding box, span and format diagnostics. Read-only. Call this FIRST to understand the cloud before detection.',
      parameters: objSchema({ cloud: strParam('Absolute path of the point cloud file') }, ['cloud']),
      meta: readOnlyMeta,
      async execute({ cloud }) {
        const structured = await runCli(['info', cloud])
        return { content: [{ type: 'text', text: JSON.stringify(structured) }], structured }
      },
    })

    agnes.registerTool({
      name: 'cloudforge_detect',
      description:
        'Run RANSAC geometric primitive detection (Schnabel qRANSAC_SD + robust refine) on a point cloud. Returns detected shapes (plane/sphere/cylinder/cone/torus) with coefficients, inlier count, RMSE, robust sigma and soft score. preset "outdoor" = planes only (architecture), "mechanical" = all five primitives (parts). Writes result.json and inlier indices into output_dir.',
      parameters: objSchema(
        {
          cloud: strParam('Absolute path of the point cloud file'),
          preset: { [Kind]: 'String', type: 'string', enum: ['outdoor', 'mechanical'], description: 'Scene preset, default mechanical' },
          min_inlier: numParam('Override minimum inlier count (0 = library default)'),
          output_dir: strParam('Absolute directory to write result.json + indices/'),
        },
        ['cloud', 'output_dir'],
      ),
      meta: writeMeta(60000),
      async execute({ cloud, preset = 'mechanical', min_inlier = 0, output_dir }) {
        const args = ['detect', cloud, '--preset', preset === 'outdoor' ? 'outdoor' : 'mechanical', '--output', output_dir]
        if (min_inlier > 0) args.push('--min-inlier', String(Math.round(min_inlier)))
        const structured = await runCli(args)
        return { content: [{ type: 'text', text: JSON.stringify(structured) }], structured }
      },
    })

    agnes.registerTool({
      name: 'cloudforge_refine',
      description:
        'Robustly re-fit geometry on a selected subset of points (e.g. one detected shape\'s inlier indices file, or an AI-segmented instance). hint narrows the primitive type (plane/sphere/cylinder/cone/torus/unknown). Refuses selections under 50 points — use that failure to pick a larger subset.',
      parameters: objSchema(
        {
          cloud: strParam('Absolute path of the point cloud file'),
          indices_file: strParam('Text file of 0-based point indices, one per line'),
          hint: { [Kind]: 'String', type: 'string', enum: ['plane', 'sphere', 'cylinder', 'cone', 'torus', 'unknown'], description: 'Primitive hint, default unknown (auto)' },
          output_dir: strParam('Optional directory to write the refined result'),
        },
        ['cloud', 'indices_file'],
      ),
      meta: writeMeta(60000),
      async execute({ cloud, indices_file, hint = 'unknown', output_dir }) {
        const args = ['refine', cloud, '--indices', indices_file, '--hint', hint]
        if (output_dir) args.push('--output', output_dir)
        const structured = await runCli(args)
        return { content: [{ type: 'text', text: JSON.stringify(structured) }], structured }
      },
    })

    agnes.registerTool({
      name: 'cloudforge_synth',
      description:
        'Generate a synthetic point cloud with KNOWN ground-truth parameters (scene plane-cylinder: plane z=0 + cylinder r=0.05m) and write groundtruth.json next to it. Use it to produce verification baselines or demo data.',
      parameters: objSchema(
        {
          output_pcd: strParam('Absolute path of the .pcd file to write'),
          scene: { [Kind]: 'String', type: 'string', enum: ['plane-cylinder'], description: 'Scene, only plane-cylinder for now' },
        },
        ['output_pcd'],
      ),
      meta: writeMeta(15000),
      async execute({ output_pcd, scene = 'plane-cylinder' }) {
        const structured = await runCli(['synth', output_pcd, '--scene', scene], { timeoutMs: 60000 })
        return { content: [{ type: 'text', text: JSON.stringify(structured) }], structured }
      },
    })

    agnes.registerTool({
      name: 'cloudforge_verify',
      description:
        'Verify detection results against ground truth: matches each truth shape to the best result shape (cylinders may match cones via equivalent radius), reports param/angle errors and per-check pass/fail. Exit pass=false means verification FAILED — report it honestly and consider re-running detection with different parameters.',
      parameters: objSchema(
        {
          truth: strParam('Ground-truth JSON (from cloudforge_synth *.truth.json)'),
          result: strParam('Detection result JSON (cloudforge_detect output_dir/result.json)'),
          tolerance: numParam('Max param error in cloud units (default 0.005)'),
          angle_tolerance_deg: numParam('Max angle error in degrees (default 2.0)'),
        },
        ['truth', 'result'],
      ),
      meta: readOnlyMeta,
      async execute({ truth, result, tolerance, angle_tolerance_deg }) {
        const args = ['verify', '--truth', truth, '--result', result]
        if (tolerance !== undefined) args.push('--tol', String(tolerance))
        if (angle_tolerance_deg !== undefined) args.push('--angle-deg', String(angle_tolerance_deg))
        const structured = await runCli(args, { timeoutMs: 60000 })
        return { content: [{ type: 'text', text: JSON.stringify(structured) }], structured }
      },
    })

    agnes.registerTool({
      name: 'cloudforge_cad_export',
      description:
        'Export detected primitives to an EXECUTABLE CadQuery Python script that builds an editable parametric CAD model (planes -> trimmed slabs, cylinders -> solid cylinders, spheres, cones -> truncated cones; sizes derived from inlier extents). Run cloudforge_detect first. The .py executes with `python <out_py>` (requires: pip install cadquery) and writes a STEP file. Filter out weak shapes with min_inlier (default 200). For large detections (30+ shapes), pass ids to export only the main structural faces — union of many slabs is slow. NEVER hand-write your own slab/geometry script: use this tool, then verify the STEP bbox against the cloud span (e.g. FreeCAD freecadcmd + hackathon/tools/freecad-verify.py) before delivering.',
      parameters: objSchema(
        {
          cloud: strParam('Absolute path of the point cloud used for detection (same file!)'),
          result_dir: strParam('Absolute directory written by cloudforge_detect (contains result.json and indices/)'),
          output_py: strParam('Absolute path of the CadQuery script to write'),
          ids: { [Kind]: 'String', type: 'string', description: 'Comma-separated shape ids to export (e.g. "0,2,3,64,56"). Omit to export all shapes passing min_inlier' },
          hull: { [Kind]: 'Boolean', type: 'boolean', description: 'Cut each plane to the convex hull of its inliers instead of a bounding rectangle (recommended: walls fit their real outline, no cross-piercing slabs)' },
          union: { [Kind]: 'Boolean', type: 'boolean', description: 'Fuse solids into one part instead of a compound (default false)' },
          min_inlier: numParam('Skip shapes with fewer inliers (default 200)'),
        },
        ['cloud', 'result_dir', 'output_py'],
      ),
      meta: writeMeta(30000),
      async execute({ cloud, result_dir, output_py, ids, hull = true, union = false, min_inlier }) {
        const args = ['cad', cloud, '--result', `${result_dir.replace(/[\\/]+$/, '')}/result.json`, '--out', output_py]
        if (ids) args.push('--ids', String(ids))
        if (hull) args.push('--hull')
        if (union) args.push('--union')
        if (min_inlier !== undefined) args.push('--min-inlier', String(Math.round(min_inlier)))
        const structured = await runCli(args, { timeoutMs: 120000 })
        return { content: [{ type: 'text', text: JSON.stringify(structured) }], structured }
      },
    })

    agnes.on('session_start', () => {
      agnes.ctx.log.info('cloudforge-tools ready: cloud_info/detect/refine/synth/verify/cad_export')
    })
  },
}
