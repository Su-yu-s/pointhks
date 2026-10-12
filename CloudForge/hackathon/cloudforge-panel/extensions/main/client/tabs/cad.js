// 云铸面板 · CAD 页签
// STEP/STL 视口（面 ↔ 线框 ↔ 叠加实测点云）+ 交付物卡 + 拉起 FreeCAD / CloudForge GUI
//
// .step 不能在浏览器里解析（要 OpenCASCADE 内核，而 CSP 无 wasm-unsafe-eval），
// 所以走服务端 freecadcmd 转 STL 并缓存 preview.stl；转换失败不阻塞面板，降级为"只给文件卡"。

import { el, icon, num, fmtSize, shortPath, esc } from '../ui.js'
import { createMeshViewer } from '../viewer.js'

export function createCadTab({ ctx, data, state }) {
  const root = el('section', { class: 'cf-pane', dataset: { pane: 'cad' } })

  const viewport = el('div', { class: 'cf-viewer' })
  const tag = el('span', { class: 'cf-viewer-tag' }, '尚未载入 CAD')
  // 空态做在**视口里**：CAD 页的大半面积就是这个画布，只在下方「模型信息」里放
  // 一行提示的话，中间仍是 vast 空白，读起来像加载失败。载入网格后此块被移除。
  const blank = el('div', { class: 'cf-blank' }, [
    el('div', { class: 'cf-blank-mark' }, [icon('cube', 20)]),
    el('div', { class: 'cf-blank-title', text: '尚未载入 CAD' }),
    el('div', { class: 'cf-blank-hint', text: '在下方「交付物」里选一个 .step / .stl，即可在此查看重建网格与尺寸校验' }),
  ])
  viewport.appendChild(blank)
  viewport.appendChild(tag)
  root.appendChild(viewport)

  const toolbar = el('div', { class: 'cf-viewer-bar' })
  let wireOn = false
  const wireBtn = el('button', { class: 'cf-btn-xs' }, '线框')
  wireBtn.addEventListener('click', () => {
    wireOn = !wireOn
    viewer?.setWireframe(wireOn)
    wireBtn.classList.toggle('on', wireOn)
  })
  toolbar.appendChild(wireBtn)

  let overlayOn = false
  const overlayBtn = el('button', { class: 'cf-btn-xs', title: '把实测点云叠到重建模型上做偏差对比' }, '叠加点云')
  overlayBtn.addEventListener('click', () => {
    overlayOn = !overlayOn
    overlayBtn.classList.toggle('on', overlayOn)
    if (overlayOn) void applyCloudOverlay()
    else viewer?.setCloudOverlay(null)
  })
  toolbar.appendChild(overlayBtn)

  for (const [name, title] of [['fit', '回到全景'], ['iso', '等轴视角']]) {
    const b = el('button', { class: 'cf-icon-btn sm', title }, [icon(name, 13)])
    b.addEventListener('click', () => {
      if (name === 'fit') viewer?.fit()
      else viewer?.stage.faceNormal([-0.55, -0.8, 0.45], [0, 0, 0], meshSpan)
    })
    toolbar.appendChild(b)
  }
  root.appendChild(toolbar)

  const scroll = el('div', { class: 'cf-scroll' })
  const infoBox = el('div', { class: 'cf-list' })
  const artBox = el('div', { class: 'cf-list' })
  scroll.appendChild(sectionTitle('模型信息'))
  scroll.appendChild(infoBox)
  scroll.appendChild(sectionTitle('交付物'))
  scroll.appendChild(artBox)
  root.appendChild(scroll)

  let viewer = null
  let meshSpan = 1
  let currentPath = null

  function sectionTitle(t) {
    return el('div', { class: 'cf-group-title', text: t })
  }

  /** 三角汤的包围盒边长（STL 直读时没有 FreeCAD sidecar，客户端自己算） */
  function meshBbox(positions) {
    if (!positions || !positions.length) return null
    let x0 = Infinity, y0 = Infinity, z0 = Infinity
    let x1 = -Infinity, y1 = -Infinity, z1 = -Infinity
    for (let i = 0; i + 2 < positions.length; i += 3) {
      const x = positions[i], y = positions[i + 1], z = positions[i + 2]
      if (x < x0) x0 = x
      if (x > x1) x1 = x
      if (y < y0) y0 = y
      if (y > y1) y1 = y
      if (z < z0) z0 = z
      if (z > z1) z1 = z
    }
    return [x1 - x0, y1 - y0, z1 - z0]
  }

  function ensureViewer() {
    if (!viewer) {
      viewer = createMeshViewer(viewport)
      viewer.visible = false
    }
    return viewer
  }

  async function applyCloudOverlay() {
    const scene = state.sceneCache
    // 要叠加的是**点云页实际载入的那一份**：source_cloud 缺失时点云页会自动兜底选一个候选，
    // 只认 scene.source_cloud 会导致叠加永远不生效
    const cloud = state.cloudPath || scene?.sourceCloud || scene?.source_cloud
    if (!cloud) return
    try {
      const loaded = await data.loadCloud(cloud, {})
      ensureViewer().setCloudOverlay(loaded.positions)
    } catch (e) {
      state.message = `点云叠加失败：${String(e.message || e)}`
    }
  }

  function renderInfo(m) {
    infoBox.replaceChildren()
    // 视口内的空态块随载入状态开关，避免「载入成功却还写着尚未载入」
    blank.classList.toggle('hidden', !!m)
    tag.classList.toggle('hidden', !m)
    if (!m) {
      // 一行灰字在 780px 高的空视口里显得很没着落。空态该说明「这里会有什么 +
      // 现在该做什么」，所以下方也做成带图标与标题的引导块。
      infoBox.appendChild(
        el('div', { class: 'cf-blank' }, [
          el('div', { class: 'cf-blank-mark' }, [icon('cube', 20)]),
          el('div', { class: 'cf-blank-title', text: '尚未载入 CAD' }),
          el('div', { class: 'cf-blank-hint', text: '在下方「交付物」里选一个 .step / .stl，即可在此查看重建网格与尺寸校验' }),
        ]),
      )
      return
    }
    // STL 直读没有 FreeCAD sidecar（体积/实体数需要内核），但包围盒可以客户端现算
    const box = m.clientBbox
    const rows = [
      ['来源', m.originFile ? shortPath(m.originFile, 40) : '—'],
      ['三角面', num(m.triCount)],
      ['包围盒', box ? `${box[0].toFixed(3)} × ${box[1].toFixed(3)} × ${box[2].toFixed(3)} m` : '—'],
      ['实体数', m.solid?.solids != null ? String(m.solid.solids) : '—（STL 无实体概念）'],
      ['体积', m.solid?.volume_m3 != null ? `${m.solid.volume_m3} m³` : '—（需 STEP + FreeCAD 内核）'],
      ['转换', m.converted ? `freecadcmd ${m.converted.cached ? '（缓存命中）' : ''} · 偏差 ${m.converted.deflection ?? '—'} m` : 'STL 直读'],
    ]
    for (const [k, v] of rows) {
      infoBox.appendChild(el('div', { class: 'cf-kv' }, [el('span', { text: k }), el('span', { class: 'mono', text: v })]))
    }
    // 覆盖自检：网格包围盒与 STEP 实体包围盒的相对偏差
    const cov = m.converted?.coverage_error ?? m.solid?.coverage_error
    if (cov !== null && cov !== undefined) {
      infoBox.appendChild(
        el('div', { class: 'cf-kv' }, [
          el('span', { text: '网格覆盖误差' }),
          el('span', { class: `mono ${cov < 0.02 ? 'ok-text' : 'err-text'}`, text: `${(cov * 100).toFixed(2)}%（${cov < 0.02 ? '完整覆盖' : '疑似缺面'}）` }),
        ]),
      )
    }
    if (m.originFile?.endsWith?.('.step')) {
      infoBox.appendChild(
        el('div', { class: 'cf-note' }, 'STEP 由 FreeCAD 内核转 STL 后预览（浏览器不解析 STEP：需 OpenCASCADE，而 CSP 禁用 wasm）'),
      )
    }
  }

  function renderArtifacts() {
    artBox.replaceChildren()
    const arts = (state.report?.artifacts ?? []).filter((a) => a.kind === 'step' || a.kind === 'stl')
    if (!arts.length) {
      artBox.appendChild(el('div', { class: 'cf-empty' }, '工作区暂无 STEP/STL 产物'))
      return
    }
    for (const a of arts) {
      const card = el('div', { class: `cf-card${currentPath === a.path ? ' active' : ''}`, dataset: { path: a.path } })
      card.appendChild(el('div', { class: 'cf-card-title' }, [a.name]))
      card.appendChild(el('div', { class: 'cf-card-meta' }, [`${a.kind.toUpperCase()} · ${fmtSize(a.size)}`]))
      const btns = el('div', { class: 'cf-btn-row' })
      const view = el('button', { class: 'cf-btn-sm primary', text: '预览' })
      view.addEventListener('click', () => void load(a.path))
      const open = el('button', { class: 'cf-btn-sm', text: 'FreeCAD 打开' })
      open.addEventListener('click', () => void launch('freecad', a))
      const gui = el('button', { class: 'cf-btn-sm', text: 'GUI 预览' })
      gui.addEventListener('click', () => void launch('cloudforge', a))
      btns.appendChild(view)
      btns.appendChild(open)
      btns.appendChild(gui)
      card.appendChild(btns)
      artBox.appendChild(card)
    }
  }

  async function launch(target, artifact) {
    state.message = `正在拉起 ${target === 'freecad' ? 'FreeCAD' : 'CloudForge GUI'}…`
    try {
      const r = await data.runEffect('cloudforge.preview', { target, file: artifact.path })
      state.message = r?.alive ? `已打开（PID ${r.pid}）` : `启动失败：${r?.error ?? '未知'}（请手动打开）`
    } catch (e) {
      state.message = `启动失败：${String(e.message || e).slice(0, 80)}`
    }
  }

  async function load(path) {
    currentPath = path
    renderArtifacts()
    const v = ensureViewer()
    v.visible = true
    v.stage.resize()
    tag.textContent = '正在载入网格…'
    state.message = '正在读取网格…'
    try {
      const m = await data.loadMesh(path, { onProgress: (p) => { tag.textContent = `载入三角面 ${p.loaded} / ${num(p.total)}` } })
      m.clientBbox = meshBbox(m.positions)
      v.setMesh(m.positions, m.triCount, m.bbox)
      v.fit()
      meshSpan = Math.max(
        m.bbox.max[0] - m.bbox.min[0],
        m.bbox.max[1] - m.bbox.min[1],
        m.bbox.max[2] - m.bbox.min[2],
        1e-3,
      )
      tag.textContent = `${m.source === 'step' ? 'STEP → STL' : 'STL'} · ${num(m.triCount)} 三角面`
      state.meshCache = m
      state.message = ''
      renderInfo(m)
    } catch (e) {
      tag.textContent = '网格不可用'
      state.message = String(e.message || e)
      renderInfo(null)
    }
  }

  return {
    el: root,
    activate() {
      ensureViewer()
      if (viewer) {
        viewer.visible = true
        viewer.stage.resize()
      }
      renderArtifacts()
      if (state.meshCache) renderInfo(state.meshCache)
      if (overlayOn) void applyCloudOverlay()
    },
    deactivate() {
      if (viewer) viewer.visible = false
    },
    render() {},
    async refresh() {
      renderArtifacts()
      if (state.meshCache) renderInfo(state.meshCache)
    },
    renderFooter(foot, { state: st }) {
      const row = el('div', { class: 'cf-foot-row' })
      row.appendChild(el('span', { class: 'mono', text: st.meshCache ? `${num(st.meshCache.triCount)} 三角面` : '未载入网格' }))
      row.appendChild(el('span', { class: 'spacer' }))
      const re = el('button', { class: 'cf-btn-sm', text: '重新转换' })
      re.addEventListener('click', () => {
        if (currentPath) void load(currentPath)
      })
      row.appendChild(re)
      foot.appendChild(row)
      if (st.message) foot.appendChild(el('div', { class: 'cf-msg', text: st.message }))
    },
    destroy() {
      viewer?.dispose()
      viewer = null
    },
  }
}