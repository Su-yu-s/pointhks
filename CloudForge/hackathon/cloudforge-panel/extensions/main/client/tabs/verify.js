// 云铸面板 · 验证页签
// 三块真实证据，不做任何编造：
//   ① 回归套件 11 项（normal 3 / boundary 3 / failure 5），detail 原文直出
//   ② verify 实跑：真值 vs 实测，参数误差 mm / 角误差 °，1e+30 哨兵显示为 —
//   ③ 鲁棒性 12 格热力：格 = σ × 保留率，颜色 = verify_pass，格内标平面误差 mm
// 点"查看"会切到点云页并高亮该基元（跨页签联动）。

import { el, num, mmOrDash, ms, typeCn, esc } from '../ui.js'

const GROUPS = [
  { id: 'normal', label: '常规链路' },
  { id: 'boundary', label: '边界条件' },
  { id: 'failure', label: '失败路径' },
]

export function createVerifyTab({ ctx, data, state }) {
  const root = el('section', { class: 'cf-pane', dataset: { pane: 'verify' } })
  const scroll = el('div', { class: 'cf-scroll' })
  root.appendChild(scroll)

  const suiteBox = el('div', { class: 'cf-list' })
  const checkBox = el('div', { class: 'cf-list' })
  const heatBox = el('div', {})
  const verdictBox = el('div', { class: 'cf-note' })

  scroll.appendChild(title('回归套件'))
  scroll.appendChild(suiteBox)
  scroll.appendChild(title('真值验证（现跑 cloudforge verify）'))
  scroll.appendChild(checkBox)
  scroll.appendChild(title('鲁棒性网格 · 噪声 σ × 保留率'))
  scroll.appendChild(heatBox)
  scroll.appendChild(verdictBox)

  function title(t) {
    return el('div', { class: 'cf-group-title', text: t })
  }

  function renderSuite() {
    suiteBox.replaceChildren()
    const suite = state.suite?.suite
    if (!suite) {
      suiteBox.appendChild(el('div', { class: 'cf-empty' }, '未找到 hackathon/tests/results/suite-result.json'))
      return
    }
    const head = el('div', { class: 'cf-kv' }, [
      el('span', { text: '总计' }),
      el('span', { class: 'mono', text: `${suite.passed}/${suite.total} 通过${suite.failed ? ` · ${suite.failed} 失败` : ''}` }),
    ])
    suiteBox.appendChild(head)
    for (const g of GROUPS) {
      const items = (suite.results ?? []).filter((r) => r.group === g.id)
      if (!items.length) continue
      const sub = el('div', { class: 'cf-sub-title' }, `${g.label} · ${items.filter((i) => i.pass).length}/${items.length}`)
      suiteBox.appendChild(sub)
      for (const item of items) {
        const dot = el('span', { class: `cf-dot-ico ${item.pass ? 'ok' : 'err'}`, text: item.pass ? '✓' : '✗' })
        const row = el('div', { class: 'cf-check', title: item.detail ?? '' }, [
          dot,
          el('span', { class: 'cf-check-name', text: item.name }),
        ])
        if (item.detail) row.appendChild(el('span', { class: 'cf-check-val', text: item.detail }))
        suiteBox.appendChild(row)
      }
    }
  }

  function renderChecks() {
    checkBox.replaceChildren()
    const v = state.verifyResult
    if (!v) {
      checkBox.appendChild(el('div', { class: 'cf-empty' }, '选择一个带真值的检测结果以验证（点下方按钮）'))
      return
    }
    if (v.ok === false) {
      checkBox.appendChild(el('div', { class: 'cf-empty' }, v.error || '验证未执行'))
      return
    }
    checkBox.appendChild(
      el('div', { class: 'cf-kv' }, [
        el('span', { text: '结论' }),
        el('span', { class: `mono ${v.pass ? 'ok-text' : 'err-text'}`, text: v.pass ? `PASS（${v.matched}/${v.total} 匹配）` : `FAIL（${v.matched}/${v.total}）` }),
      ]),
    )
    checkBox.appendChild(
      el('div', { class: 'cf-kv' }, [
        el('span', { text: '阈值' }),
        el('span', { class: 'mono', text: `参数 ${mmOrDash(v.tolerance)} · 角度 ${v.angle_tolerance_deg}°` }),
      ]),
    )
    for (const c of v.checks ?? []) {
      const row = el('div', { class: 'cf-check' }, [
        el('span', { class: `cf-dot-ico ${c.pass ? 'ok' : 'err'}`, text: c.pass ? '✓' : '✗' }),
        el('span', { class: 'cf-check-name', text: `真值#${c.truth_id} ${typeCn(c.type)}${c.match === 'generalized' ? '（泛化匹配）' : ''}` }),
        el('span', { class: 'cf-check-val', text: `Δ ${mmOrDash(c.param_error)} · ${(c.angle_error_deg ?? 0).toFixed(3)}°` }),
      ])
      row.classList.add('clickable')
      row.addEventListener('click', () => {
        // 跳到点云页并高亮对应检测形状
        const shapeId = c.matched_shape
        state.pendingHighlight = shapeId
        window.__CF_DEBUG?.selectTab?.('cloud')
        state.message = shapeId == null ? '' : `已定位检测形状 #${shapeId}`
      })
      checkBox.appendChild(row)
    }
  }

  /** 哨兵值：云铸用 1e+30 表示「未检出 / 拟合失控」，不是真实测量值。
 *  必须先判掉再格式化——Number.prototype.toFixed 对 ≥1e21 的数会**回退成指数记法**
 *  （(1e+30*1000).toFixed(2) === "1.0000000000000001e+33"），界面上就会出现这串天文数字。 */
const SENTINEL = 1e21

/** 把可能是哨兵值的米制误差转成毫米；无结果返回 null（调用方显示 —）。 */
function errMm(v) {
  if (typeof v !== 'number' || !Number.isFinite(v) || Math.abs(v) >= SENTINEL) return null
  return v * 1000
}

function renderHeat() {
    heatBox.replaceChildren()
    const rb = state.suite?.robustness
    if (!rb?.grid?.length) {
      heatBox.appendChild(el('div', { class: 'cf-empty' }, '未找到 robustness.json'))
      verdictBox.textContent = ''
      return
    }
    const grid = el('div', { class: 'cf-heat' })
    for (const g of rb.grid) {
      const pass = g.verify_pass
      const plane = errMm(g.plane_err_m)
      const cyl = errMm(g.cyl_err_m)
      const cell = el('div', {
        class: `cf-heat-cell ${pass ? 'is-pass' : 'is-fail'}`,
        title:
          `噪声 σ=${(g.noise_sigma * 1000).toFixed(0)}mm · 保留 ${(g.keep_ratio * 100).toFixed(0)}% · ${num(g.points)} 点 · ${g.shapes} 形状\n` +
          `平面误差 ${plane != null ? plane.toFixed(3) + ' mm' : '未检出'} · 圆柱误差 ${cyl != null ? cyl.toFixed(3) + ' mm' : '未检出'}`,
      })
      // 主数值：平面误差。哨兵值显示「未检出」而不是 1e+33。
      cell.appendChild(el('b', { class: 'cf-heat-num', text: plane != null ? plane.toFixed(2) : '未检出' }))
      cell.appendChild(el('span', { class: 'cf-heat-unit', text: plane != null ? 'mm 平面' : '' }))
      cell.appendChild(
        el('span', {
          class: 'cf-heat-meta',
          text: `σ${(g.noise_sigma * 1000).toFixed(0)} · 保留${(g.keep_ratio * 100).toFixed(0)}%`,
        }),
      )
      grid.appendChild(cell)
    }
    heatBox.appendChild(grid)
    verdictBox.textContent = rb.verdict ?? ''
  }

  async function runVerify() {
    // 优先用当前工作流；它没有真值时自动改用最近一条带真值的工作流，并在界面上说明
    let wf = state.workflow
    if (!wf?.truthPath) {
      wf = (state.report?.workflows ?? []).find((w) => w.truthPath)
      if (wf) {
        state.message = `当前工作流无真值，改用 ${wf.name}（合成基准）`
      }
    }
    if (!wf) {
      state.message = '工作区没有带真值（*.truth.json）的检测结果，无法 verify'
      return
    }
    const truth = wf.truthPath
    state.message = `正在跑 verify：${wf.name}…`
    try {
      state.verifyResult = await data.verify(`${wf.dir}/result.json`, truth)
      state.verifyTruth = truth
      state.message = state.verifyResult?.pass
        ? `verify PASS（${state.verifyResult.matched}/${state.verifyResult.total} 匹配）`
        : `verify FAIL（${state.verifyResult?.matched ?? 0}/${state.verifyResult?.total ?? 0}）`
    } catch (e) {
      state.message = `verify 失败：${String(e.message || e).slice(0, 100)}`
    }
    renderChecks()
  }

  return {
    el: root,
    activate() {
      renderSuite()
      renderChecks()
      renderHeat()
    },
    deactivate() {},
    render() {},
    async refresh() {
      try {
        state.suite = await data.suite()
      } catch (e) {
        state.message = `套件读取失败：${String(e.message || e).slice(0, 80)}`
      }
      const wf = state.workflow
      state.verifyTruth = wf?.truthPath ?? null
      if (!state.verifyTruth) {
        const cand = (state.report?.workflows ?? []).find((w) => w.truthPath)
        if (cand) state.message = `「${wf?.name ?? '当前'}」无真值；点「跑 verify」将自动改用 ${cand.name}`
      }
    },
    renderFooter(foot, { state: st }) {
      const row = el('div', { class: 'cf-foot-row' })
      const v = st.verifyResult
      row.appendChild(el('span', { class: 'mono', text: v?.pass ? `verify PASS ${v.matched}/${v.total}` : 'verify 未运行' }))
      row.appendChild(el('span', { class: 'spacer' }))
      const btn = el('button', { class: 'cf-btn-sm primary', text: '跑 verify' })
      btn.addEventListener('click', () => void runVerify())
      row.appendChild(btn)
      foot.appendChild(row)
      if (st.message) foot.appendChild(el('div', { class: 'cf-msg', text: st.message }))
    },
  }
}