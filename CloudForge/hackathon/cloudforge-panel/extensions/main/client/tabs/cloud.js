// 云铸面板 · 点云页签
// 视口 + HUD + 四视图模式 + 几何网格叠加 + 形状清单（点击高亮 / 显隐 / solo）
//
// 内点着色不额外请求 indices/shape_k.txt（38 个形状要 38 次调用），而是按几何距离就地筛：
// 判据 |n·p+d| ≤ 2.5·robust_sigma（平面）/ |到轴距 − r| ≤ 2.5σ（圆柱），与 RANSAC 自身判据同源。
// 实测校验：livingroom shape_1 距离筛得 43,276 点，真实 inlier_count 43,558（吻合 99.3%）。

import { el, icon, num, mm, pct, ms, typeCn, esc, fmtSize } from '../ui.js'
import { createCloudViewer, PALETTE } from '../viewer.js'
import { buildPrimitive, pointDistance } from '../geometry.js'

const MODES = [
  { id: 'detect', label: '检测' },
  { id: 'raw', label: '原始' },
  { id: 'inliers', label: '内点' },
  { id: 'remaining', label: '剩余' },
]
const SIGMA_K = 2.5
const SIGMA_CAP = 0.05 // σ 异常大时封顶，避免整团点被染成一个形状

export function createCloudTab({ ctx, data, state }) {
  const root = el('section', { class: 'cf-pane', dataset: { pane: 'cloud' } })
  let reloadTab = async () => {}

  const viewport = el('div', { class: 'cf-viewer' })
  const tag = el('span', { class: 'cf-viewer-tag' }, '尚未载入点云')
  const legend = el('div', { class: 'cf-legend' })
  viewport.appendChild(tag)
  viewport.appendChild(legend)
  root.appendChild(viewport)

  const toolbar = el('div', { class: 'cf-viewer-bar' })
  const modeBar = el('div', { class: 'cf-seg' })
  const modeButtons = new Map()
  for (const m of MODES) {
    const b = el('button', { class: 'cf-seg-btn', dataset: { mode: m.id }, text: m.label })
    b.addEventListener('click', () => {
      viewer?.setMode(m.id)
      syncModes()
    })
    modeBar.appendChild(b)
    modeButtons.set(m.id, b)
  }
  toolbar.appendChild(modeBar)

  const overlayBtn = el('button', { class: 'cf-btn-xs', title: '把 coefficients 变成半透明实体网格' }, '几何叠加')
  // 与 viewer.js 的 overlayOn 保持一致：默认关闭。43 个面片叠加会把点云本体
  // 完全糊住，叠加是「按需」信息层而不是默认主体。
  let overlayOn = false
  overlayBtn.addEventListener('click', () => {
    overlayOn = !overlayOn
    viewer?.setOverlayVisible(overlayOn)
    overlayBtn.classList.toggle('on', overlayOn)
  })
  toolbar.appendChild(overlayBtn)

  for (const [name, title] of [
    ['fit', '回到全景'],
    ['iso', '等轴视角'],
    ['top', '顶视图'],
    ['front', '前视图'],
  ]) {
    const b = el('button', { class: 'cf-icon-btn sm', title }, [icon(name, 13)])
    b.addEventListener('click', () => {
      if (!viewer) return
      if (name === 'fit') viewer.fit()
      else if (name === 'iso') viewer.stage.faceNormal([-0.55, -0.8, 0.45], centerOf(), spanOf())
      else if (name === 'top') viewer.stage.faceNormal([0, 0, 1], centerOf(), spanOf())
      else if (name === 'front') viewer.stage.faceNormal([0, -1, 0], centerOf(), spanOf())
    })
    toolbar.appendChild(b)
  }
  root.appendChild(toolbar)

  const listWrap = el('div', { class: 'cf-scroll' })
  const picker = el('div', { class: 'cf-picker' })
  const shapeList = el('div', { class: 'cf-list' })
  listWrap.appendChild(picker)
  listWrap.appendChild(shapeList)
  root.appendChild(listWrap)

  let viewer = null
  let scene = null
  let basePoints = null
  let bbox = null
  let span = 1
  let activeId = null
  let hidden = new Set()

  const centerOf = () => (bbox ? [(bbox.min[0] + bbox.max[0]) / 2, (bbox.min[1] + bbox.max[1]) / 2, (bbox.min[2] + bbox.max[2]) / 2] : [0, 0, 0])
  const spanOf = () => span

  function ensureViewer() {
    if (viewer) return viewer
    viewer = createCloudViewer(viewport, {
      onPickShape: (ray) => {
        const id = viewer.pickShapeId(ray)
        setActive(id === activeId ? null : id)
      },
    })
    return viewer
  }

  function setActive(id) {
    activeId = id
    viewer?.highlight(id)
    renderList()
    if (id !== null) {
      const s = (scene?.shapes ?? []).find((x) => x.id === id)
      if (s) state.message = `#${id} ${typeCn(s.type)} · ${num(s.inlier_count)} 内点 · RMSE ${mm(s.rmse)} · σ ${mm(s.robust_sigma)}`
      viewer?.focusShape(id)
    } else if (state.message.startsWith('#')) {
      state.message = ''
    }
  }

  function syncModes() {
    for (const [id, b] of modeButtons) b.classList.toggle('on', id === (viewer?.mode ?? 'detect'))
  }

  function renderLegend() {
    legend.replaceChildren()
    // data-pal 对应 index.css 里的 .cf-swatch[data-pal=…]，颜色写死在 CSS 里（CSP 禁 inline style）
    const rows = [
      ['base', '原始点云'],
      ['p0', '检测基元（按 id 循环 7 色）'],
      ['overlay', '几何叠加网格'],
      ['remaining', '未归类点'],
    ]
    for (const [pal, label] of rows) {
      legend.appendChild(
        el('div', { class: 'cf-legend-row' }, [
          el('i', { class: 'cf-swatch', dataset: { pal } }),
          el('span', { text: label }),
        ]),
      )
    }
  }

  function renderList() {
    shapeList.replaceChildren()
    const shapes = scene?.shapes ?? []
    if (!shapes.length) {
      shapeList.appendChild(el('div', { class: 'cf-empty' }, '暂无检测结果——先在会话里跑一次分析'))
      return
    }
    for (const s of shapes) {
      const row = el('div', {
        class: `cf-row${activeId === s.id ? ' active' : ''}${hidden.has(s.id) ? ' dim' : ''}`,
        dataset: { id: String(s.id) },
      })
      const sw = el('i', { class: 'cf-swatch', dataset: { pal: `p${s.id % PALETTE.length}` } })
      const name = el('div', { class: 'cf-row-main' }, [
        el('div', { class: 'cf-row-title' }, [
          el('span', { class: 'cf-badge', text: typeCn(s.type) }),
          el('span', { text: `#${s.id}` }),
        ]),
        el('div', { class: 'cf-row-meta' },
          `${num(s.inlier_count)} 内点 · RMSE ${mm(s.rmse)} · σ ${mm(s.robust_sigma)}`),
        // soft_score 分 21 档（每档 5%）：精确数值就印在上面的 meta 行，
        // 条形只要能看出健康度即可，这样不必用被 CSP 禁掉的 inline width
        el('div', { class: 'cf-bar' }, [
          el('i', { dataset: { score: String(Math.round(Math.min(1, Math.max(0, s.soft_score ?? 0)) * 20)) } }),
        ]),
      ])
      // 「只看这个」用图标而不是文字：43 行列表里每行都一个 "solo" 文字按钮，
      // 重复 43 次的文案会变成噪音；图标 + title 保留可发现性与悬停说明。
      const solo = el('button', { class: 'cf-btn-xs cf-solo', title: '只看这个基元', 'aria-label': `只看基元 #${s.id}` }, [icon('iso', 12)])
      solo.addEventListener('click', (ev) => {
        ev.stopPropagation()
        hidden = new Set(shapes.filter((x) => x.id !== s.id).map((x) => x.id))
        setActive(s.id)
      })
      row.appendChild(sw)
      row.appendChild(name)
      row.appendChild(solo)
      row.addEventListener('click', () => {
        if (hidden.has(s.id)) hidden.delete(s.id)
        setActive(activeId === s.id ? null : s.id)
      })
      shapeList.appendChild(row)
    }
  }

  function updateTag() {
    if (!scene) {
      tag.textContent = '尚未载入点云'
      return
    }
    // 点云名取实际载入的那份（source_cloud 缺失时可能是自动兜底或用户在兜底选择器里挑的）
    const cloudName = (state.cloudPath || scene.sourceCloud || scene.source_cloud || '').split(/[\\/]/).pop()
    // 点数取**实际载入的这份点云**，不是 result.json 里的 input_point_count：
    // 兜底载入时两者不同，只报后者会变成假数字
    const shown = basePoints ? Math.floor(basePoints.length / 3) : null
    const declared = scene.input_point_count
    const cov = declared ? scene.inlier_point_sum / declared : null
    tag.textContent =
      `${cloudName || '点云'} · ${num(shown ?? declared)} 点 · ${scene.shape_count} 基元` +
      (shown != null && declared && shown !== declared ? `（检测输入 ${num(declared)} 点）` : '') +
      (cov != null ? ` · 覆盖 ${pct(cov)}` : '') +
      (scene.elapsed_ms != null ? ` · ${ms(scene.elapsed_ms)}` : '')
  }

  /** 按几何距离切出每个形状的内点子集（收集 xyz 三元组，不是下标） */
  function splitInliers(positions, shapes) {
    const out = []
    const count = Math.floor(positions.length / 3)
    for (const s of shapes) {
      const tol = Math.min(SIGMA_K * (s.robust_sigma || 0.002), SIGMA_CAP)
      const picked = []
      for (let i = 0; i < count; i++) {
        const j = i * 3
        if (pointDistance(s, positions[j], positions[j + 1], positions[j + 2]) <= tol) {
          picked.push(positions[j], positions[j + 1], positions[j + 2])
        }
      }
      out.push(picked.length >= 3 ? Float32Array.from(picked) : null)
    }
    return out
  }

  /** 载入当前工作流的检测结果与点云（source_cloud 缺失时给兜底选择器，绝不把 phase 卡在 loading） */
  async function loadScene() {
    const wf = state.workflow
    if (!wf) {
      scene = null
      basePoints = null
      bbox = null
      picker.replaceChildren()
      renderList()
      updateTag()
      state.phase = 'empty'
      return
    }
    state.phase = 'loading'
    let s
    try {
      s = await data.scene(wf.dir)
      if (s.ok === false) throw new Error(s.error)
    } catch (e) {
      state.phase = 'empty'
      state.message = `读取检测结果失败：${String(e.message || e)}`
      return
    }
    scene = s
    state.sceneCache = s // CAD 页要用源点云做叠加
    let cloud = state.cloudOverride || s.sourceCloud || s.source_cloud
    // source_cloud 缺失时不留空白视口：自动取最近的一个候选（candidateClouds 已按目录就近排序），
    // 并保留选择器让用户换文件。内点仍按几何重投影切分，不用原输入的下标。
    const autoPicked = !cloud && !!s.candidateClouds?.length
    if (autoPicked) cloud = s.candidateClouds[0].path
    if (!cloud) {
      state.phase = 'ready'
      state.message = `「${wf.name}」未记录源点云路径，请在下方选择要显示的点云`
      renderPicker(s.candidateClouds ?? [])
      renderList()
      updateTag()
      return
    }
    const v = ensureViewer()
    state.progress = { loaded: 0, total: null }
    try {
      const loaded = await data.loadCloud(cloud, {
        onPage: (positions, loadedCount, page) => {
          // 进度基准必须是**这份点云自己的** total：source_cloud 缺失时用户可能选了别的点云，
          // 拿 scene.input_point_count 当分母会出现 "250000 / 196133" 这种越界显示
          state.progress = { loaded: loadedCount, total: page.total }
          state.message = `载入点云 ${num(loadedCount)} / ${num(page.total)}`
        },
      })
      basePoints = loaded.positions
      bbox = loaded.bbox ?? { min: [0, 0, 0], max: [0, 0, 0] }
      span = loaded.span || 1
      v.setBase(basePoints, bbox)

      const prims = (scene.shapes ?? []).map((sh) => buildPrimitive(sh, bbox))
      const subsets = splitInliers(basePoints, scene.shapes ?? [])
      v.setShapes(prims.map((prim, i) => ({ prim, positions: subsets[i] })))
      v.setRemaining(null)
      v.setMode(v.mode ?? 'detect')
      v.fit()
      syncModes()
      state.phase = 'ready'
      state.cloudPath = cloud
      state.cloudOverride = null
      if (autoPicked) {
        state.message = `result.json 未记录 source_cloud，已自动载入最近候选 ${cloud.split(/[\\/]/).pop()}（内点按几何重投影切分）`
        renderPicker(s.candidateClouds ?? [])
      } else {
        state.message = ''
        picker.replaceChildren()
      }
      // 验证页点某个 check 时会请求高亮对应形状
      if (state.pendingHighlight != null) {
        setActive(state.pendingHighlight)
        state.pendingHighlight = null
      }
    } catch (e) {
      state.phase = 'empty'
      state.message = `点云载入失败：${String(e.message || e)}`
    }
  }

  /** source_cloud 缺失时的兜底点云选择器 */
  function renderPicker(candidates) {
    picker.replaceChildren()
    if (!candidates?.length) {
      picker.appendChild(el('div', { class: 'cf-note', text: '工作区也没找到 .ply / .pcd，请先在会话里跑一次分析' }))
      return
    }
    const row = el('div', { class: 'cf-foot-row' })
    row.appendChild(el('span', { class: 'dim', text: '点云' }))
    const sel = el('select', { class: 'cf-select' })
    for (const c of candidates) sel.appendChild(el('option', { value: c.path, text: `${c.name}（${fmtSize(c.size)}）` }))
    sel.addEventListener('change', () => {
      state.cloudOverride = sel.value
      void reloadTab()
    })
    row.appendChild(sel)
    picker.appendChild(row)
  }

  return {
    el: root,
    viewer: () => viewer,
    state: () => ({ scene, bbox, span, activeId, basePoints }),
    /** 面板外壳注入：点云选择器换源后要重跑本页签 */
    setReload(fn) {
      reloadTab = fn
    },

    activate() {
      ensureViewer()
      viewer.visible = true
      viewer.stage.resize()
      syncModes()
    },
    deactivate() {
      if (viewer) viewer.visible = false
    },

    render() {
      updateTag()
      renderList()
    },

    async refresh() {
      await loadScene()
    },

    /** 底部工具条：来源 + 进度 + 视图模式 + 键盘提示 */
    renderFooter(foot, { state: st, data: dl, reload }) {
      const row = el('div', { class: 'cf-foot-row' })
      const wf = st.workflow
      row.appendChild(el('span', { class: 'mono', text: wf ? `${wf.name}` : '无工作流' }))
      row.appendChild(el('span', { class: 'sep', text: '·' }))
      row.appendChild(el('span', { class: 'mono', text: `${num(wf?.points)} 点 / ${wf?.shapes ?? 0} 基元` }))
      const spacer = el('span', { class: 'spacer' })
      row.appendChild(spacer)
      const reloadBtn = el('button', { class: 'cf-btn-sm', text: '重新载入' })
      reloadBtn.addEventListener('click', () => void reload())
      row.appendChild(reloadBtn)
      foot.appendChild(row)

      if (st.progress && st.progress.total) {
        // 用 <progress> 的 value/max **属性**而不是 inline width：CSP 的 style-src 不含
        // 'unsafe-inline'，style 属性会被浏览器拦掉
        const p = document.createElement('progress')
        p.className = 'cf-progress'
        p.max = st.progress.total
        p.value = Math.min(st.progress.loaded, st.progress.total)
        foot.appendChild(p)
        foot.appendChild(
          el('div', { class: 'cf-foot-row' }, [
            el('span', { class: 'mono', text: `已载入 ${num(st.progress.loaded)} / ${num(st.progress.total)}` }),
            el('span', { class: 'spacer' }),
            el('span', { class: 'dim', text: 'uint16 量化 · 单批 65,536 点 · 服务端上限 1 MiB' }),
          ]),
        )
      }
      if (st.message) foot.appendChild(el('div', { class: 'cf-msg', text: st.message }))
    },

    destroy() {
      viewer?.dispose()
      viewer = null
    },
  }
}