# CloudForge 右栏面板 · AGH 集成说明（PANEL-AGH）

> 本文是**实现约束与复现步骤**的权威说明，回答"这个面板在 AGH 里到底怎么跑起来的"。
> 早期设计稿见 [`../PANEL-PLAN.md`](../PANEL-PLAN.md)，其中"AGH 无全屏面板 slot"的结论**已过时**（见 §1）。
> 不依赖 AGH 的离线预览台见 [`tools/`](tools/)。

## 1. 形态：注册 `workbench.panel`，自己做右侧浮动卡片

旧版面板用 `position:fixed` 盖在 `body` 上，会与宿主自己的
`[data-agnes-region="rightbar"]` 浮层卡片（`packages/web/public/style.css:1288-1298`）位置打架。
本版仍是自己做浮层卡片，但**挂载点换成宿主为客户端模块预留的槽**：

```jsonc
// hackathon/cloudforge-panel/extensions/main/agnes.client.json
{ "client": {
  "entry": "./client/index.js",
  "styles": ["./client/index.css"],
  "slots": ["workbench.panel"],                // 只声明真正注册的槽，否则 reconcile 报 unsupported-slot
  "slotCatalogVersion": "dsh-client-slots/v1", // 缺这一项 client-module.ts:174 直接拒
  "services": [ /* 12 个，见 §3 */ ],
  "projections": [] } }
```

| 事实 | 依据 |
|---|---|
| `workbench.panel` 是 `{kind:'list', scope:'session-maybe'}` 的 legacy 客户端模块槽 | `web-client/src/slots.ts:44,167-171` |
| 宿主把 `panelContainer` 定为 `#main-content`，在里面建 `<div data-agnes-slot-mount="workbench.panel">` 并渲染 `SlotOutlet({hideWhenEmpty:true})` | `web/src/app.ts:244`、`web/src/client-modules/boot.ts:160-176` |
| 该挂载点只是普通流容器，**没有任何右栏样式** | 实测：`#main-content` 内一个 `div.cf-host` |
| `session-maybe` ⇒ 草稿态（无活动会话）也会挂载 | 槽声明 |
| **`rightbar.session` 不可用**：它是宿主自有的会话右栏，已被内置单元占用，第三方注册不产生渲染 | `web/src/region-slots.ts:796,859`；实测右栏 416px 内只有 `[data-agnes-region-unit=rightbar]` 的"扩展面板/暂无指南" |
| 右栏页签 `sidebar.right.pane.tab` 的 `tabId` 是封闭联合 `'document' \| 'guide'`，没有第三方页签入口 | `web-client/src/slots.ts:83-92` |
| `slots` 必须 public 且在目录里，否则注册抛错 | `web-client/src/client-module.ts:170-180` |

因为挂载点不给右栏样式，**宽度/定位/高度都由面板 CSS 自己负责**（A1 方案）：
`position:fixed; top:72px; right:16px; width:min(26rem,100vw-2rem);
height:calc(100dvh - 72px - 16px)`，1rem 圆角浮层卡片。
高度用一条 CSS `calc` 解决，**没有 JS 高度链**——写 `element.style` 会被 CSP 拦（见 §6）。

**面板不改宿主宽度、不拖拽、不归零折叠**：宽度就是 `min(26rem, …)` ≈ 416px。
折叠是面板**内部**收成 44px 轨道（`data-rail="1"`）。
注意它会浮在宿主右栏卡片之上（宿主右栏默认只有"暂无指南"）；收成轨道后基本不挡事。

## 2. 组件形态：React 只出一个容器，子树全命令式

AGH 的插槽渲染器把 component 当 React 组件调用
（`web-client/src/outlet.tsx:269 createElement(Comp, props)`），返回 DOM 节点会直接报错。
所以 `client/index.js` 只用 `createElement` 出**一个 `div.cf-host`**，整个面板子树命令式构建——
three.js 需要真实 DOM 节点与 canvas，交给 React 更麻烦。`'react'` 是 AGH import map
放行的 8 个裸标识符之一（`web/public/index.html:18-31`）。

⚠️ **挂载时序陷阱**：`ref` 回调触发时容器**还没插入 DOM**，此时
`node.closest('[data-agnes-region="rightbar"]')` 返回 `null`、`clientHeight` 为 `0`。
高度链必须惰性解析并在挂载后补测（`client/panel.js` 的 `findAside()` + `requestAnimationFrame`）。

高度取值要用 `clientHeight` **减去 padding**（宿主卡片 `padding:1rem`），否则宿主
`overflow:auto` 会多出一截外层滚动条。

## 3. 服务：12 个声明（白名单即通道）

`ctx.agnes.services.call(name, input)` 是浏览器→后端唯一通道，硬约束
（`packages/protocol/gen/ts/extension-service.ts:8`）：

| 约束 | 值 |
|---|---|
| `maxResultBytes` | ≤ 1,048,576 |
| `timeoutMs` | ≤ 30,000 |
| `kind` | 仅 `'query'` \| `'effect'` |
| `outputSchema` | 必填，且**每次调用都校验**（`ext-host/service-invocation.ts:169-170`） |

| 服务 | kind | 用途 |
|---|---|---|
| `cloudforge.report` | query | 工作流 / 交付物 / 套件摘要总览 |
| `cloudforge.cloud` | query | 点云分页（uint16 量化） |
| `cloudforge.cloudmeta` | query | 点云元信息（格式诊断） |
| `cloudforge.scene` | query | 检测结果（形状 / 系数 / 覆盖统计） |
| `cloudforge.mesh` | query | 网格分页；`.step` 经 freecadcmd 转 STL 并缓存 |
| `cloudforge.verify` | query | **现跑** `cloudforge-cli verify`（仓库无落盘 checks） |
| `cloudforge.suite` | query | 回归套件 + 鲁棒性网格 |
| `cloudforge.trace` | query | 解析 AGH 事件 JSONL → 执行链（离线摘要/兜底） |
| `cloudforge.run` | effect | 面板调 CLI（写操作） |
| `cloudforge.preview` | effect | 拉起 FreeCAD / CloudForge GUI |
| `cloudforge.export` | effect | 调 AGH 官方 `agnes export` 导出执行记录 |
| `cloudforge.exportstatus` | query | 导出进度（导出是异步的，见 §5） |

浏览器侧还要过 `agnes.client.json` 的 `services` 白名单，少一个就抛
`client service is unavailable`（`client-module.ts:83-84`）。

### 传输预算（为什么是 65,536 点/批）

base64 要 ×4/3，于是：

```
uint16 量化 xyz = 6 B/点 → 65,536 点 = 393,216 B → base64 524,288 字符 ≈ 512 KB ✓
196,133 点（livingroom）→ 3 批（65536 + 65536 + 65061）
三角汤 9 分量/面 = 18 B/面 → 65,536 面 = 1,179,648 B → base64 超限，故 mesh 同样按 65,536 面分页
```

量化精度：`span/65535`。livingroom span 3.4075 m → **0.052 mm**，远细于 `robust_sigma`（mm 级）。

## 4. 写操作：effect 必须注册成命令

`services.call` 只能到 query。写操作走命令系统（`client-module.ts:114-130`）：

```js
ctx.commands.registerEffect({ id: 'cloudforge.detect', title: '重跑检测', service: 'cloudforge.run' })
await ctx.commands.execute('cloudforge.detect', { op: 'detect', args: [...] })
  // → executeOwned → authorize → window.confirm → POST /api/client-modules/effect
```

- 命令 id 必须匹配 `/^[a-z][a-z0-9.-]{0,127}$/`（`web-client/src/services.ts:144`）
- **授权是浏览器原生 `window.confirm`**，固定文案（`web/src/app.ts:240-243`）：
  `是否允许插件 … 执行命令"重跑检测"（服务：cloudforge.run）？`
- `executeOwned` 是 owner-scoped，只能执行本 row 注册的命令

## 5. turn-end 自动刷新

AGH **没有**自定义事件总线。实时链路是
`ctx.session.projection.subscribe()` + `projection.read().opState`，`opState` 由非 null 回 null 即一轮结束
（`web-client/src/services.ts:246-299`）。`client/data.js` 的 `onTurnEnd()` 实现了这个判据。

导出 AGH 执行记录走**官方命令**，不自研格式：

```
agnes export <sessionId> --raw -o <file>       # JSONL，与 hackathon/evidence/*.json 同格式
agnes export <sessionId> --html --raw -o <f>   # 自包含 HTML
```

因服务超时上限 30s 而整轮 export 可能更久，`cloudforge.export` 采用**启动即返回 + query 轮询**
两段式（`job` id → `cloudforge.exportstatus`）。默认加 `--raw`：参赛证据要保留
`toolUseId` / 审批策略等原始字段，脱敏后无法核查。

## 6. 资产与 CSP 约束

| 约束 | 后果 | 对策 |
|---|---|---|
| `script-src 'self'`（无 `unsafe-inline`） | 不能写 `<style>` / `eval` | CSS 走 descriptor `styles`（同源 `<link>`） |
| `style-src 'self' 'nonce-…'`——**只有 index.html 带 nonce**，`/plugins/*` 是裸 `'self'`（`web-server/src/server.ts:291/611`） | `element.style.x=`、`cssText`、`setAttribute('style')` 全被浏览器拦，每处刷一条 console error（实测首屏 87 条） | 离散值 → class / `data-*`；连续宽度 → `<progress>` 的 `value`/`max` **属性**。`el()` 遇到 `style:` 直接 throw，免得静默违规 |
| `script-src` 无 `'wasm-unsafe-eval'` | 不能用 Draco / Basis / OCCT.js | PLY/PCD/STL 自己在 Node 端解 |
| `/plugins` 只服务 `.js/.mjs/.css/.map` | 拿不到 `.wasm` / `.glb` / 图片 | 三维数据走后端服务（base64 + 分页） |
| 单文件 ≤ 4 MiB，整包 ≤ 16 MiB | 快照能收下，但**发不出来** | 见下一行 |
| **每个 client 资源的 RPC 结果 JSON ≤ 1 MiB**（`packages/protocol/src/package-admin.ts:605` 的 `inspectJsonData(value, 1048576)`），base64 又涨 4/3 | 原始字节必须 ≲ 780 KB。`three.core.js` 1,403,455 B → base64 1,871,272 B，daemon 抛 `RESULT_INVALID: expected bounded strict JSON data`，web 层统一变成**一个光秃秃的 404**（同目录其它文件全 200，极难定位） | esbuild 压到 381,420 B（base64 508,560 B，占预算 49%）。压缩只换内容、**不改文件名**，因为 `three.module.js` 里是 `from './three.core.js'` |
| 资产快照**硬过滤**只收以 `.` 开头的相对引用（`daemon/src/packages/client-modules.ts:780`） | 裸 `three` 不会被收集 → 浏览器白屏 | 两个 addon 的 `from 'three'` 改成 `from '../../three.module.js'` |
| import map 只放行 8 个裸标识符 | 不能 `import * as THREE from 'three'` | 同上 |

three 副本落在 `client/vendor/`，**已 minify**（`three.core.js` 381 KB + `three.module.js` 341 KB +
`OrbitControls.js` 38 KB），随包分发，MIT 许可头保留在文件头，披露见 [`../declaration.md`](../declaration.md)。
重新压缩：

```powershell
esbuild client\vendor\three.core.js   --minify --format=esm --target=es2022 --outfile=three.core.min.js
esbuild client\vendor\three.module.js --minify --format=esm --target=es2022 --outfile=three.module.min.js
# 然后覆盖回原文件名（import 说明符写死了 './three.core.js'）
```

## 7. 视觉与交互契约（对齐 `src/viewer.ts`）

| 项 | 值 |
|---|---|
| 底云 / 检测底云 / remaining | `0x56606e` / `0xb2b8c2` / `0x808996` |
| 形状色 | `PALETTE[i % 7]` |
| 选中 | alpha 1.0 · size 4 · `depthTest=false` · `renderOrder=900`；未选 alpha 0.18 |
| 绘制顺序 | 底云 0 → 形状 2+i → remaining 1000 → 叠加网格 500+i |
| 取景 | center=质心 · `distance=span*1.5` · `near=span*0.001` · `far=span*20` |
| 缩放夹取 | `[span*0.1, span*30]` |
| 降采样 | >850,000 点抽稀到 ~750,000 |
| 坐标系 | Z-up（PLY/PCD 保留原采集坐标系 `centered=false`），`camera.up=(0,0,1)` |

内点着色**不额外请求** `indices/shape_k.txt`（38 个形状要 38 次调用），而按几何距离就地筛：
`|n·p+d| ≤ 2.5·robust_sigma`（平面）、`|到轴距 − r| ≤ 2.5σ`（圆柱），与 RANSAC 自身判据同源。
实测校验：livingroom shape_1 距离筛得 43,276 点，真实 `inlier_count` 43,558（吻合 99.3%）。

几何网格叠加的尺寸来源与 `hackathon/cli/main.cpp` 的 `cad` 子命令一致
（平面 `t = max(0.005, 2·rmse)`、圆柱沿轴投影 `[tmin,tmax]`、圆锥 `k=tan(halfAngle)`），
否则 CAD 页与 STEP 交付物对不上。Torus 用包围盒线框 + "近似"标注（CLI 的 `cad` 也不导出该类型）。

## 8. 已知实现陷阱（都是本次实际踩过的）

| # | 陷阱 | 后果 | 修法 |
|---|---|---|---|
| 1 | 分页终止条件拿**块数**和**点数**比 | 无限翻页 | 用已载入**点数**：`loadedPoints >= page.total` |
| 2 | `splitInliers` 把**下标**当坐标塞进数组，且长度不能被 3 整除 | `BufferAttribute.count` 变小数 → three 越界读 `undefined` → NaN 半径，连报 27 次 | 收集 xyz 三元组；`toPoints` 再加一道 3 整除防御 |
| 3 | `parseStl` 把 `count` 设成**面数** | `boundsOf` 只扫 1/3 顶点 → 包围盒 Z 长 0.2056 m 只解出 0.1126 m，网格被量化截断 | `count` 用顶点数，面数另存 `faces` |
| 4 | `encodePage` 写死**每项 3 个分量** | 网格每面 9 分量只编码 1/3 顶点，三角汤残缺 | 加 `compsPerItem` 参数（点云 3 / 三角面 9） |
| 5 | `MeshPart.meshFromShape` 在复合体上退化 | STEP 形状 Z 长 0.2056 m，网格只有 0.0106 m，**预览缺一截却看不出来** | 改用 `Shape.tessellate()` 自己写二进制 STL，并输出 `coverage_error` 自检 |
| 6 | freecadcmd 脚本写 `if __name__ == "__main__"` 守卫 | 守卫不成立 → 脚本体不执行 → STEP 被当待打开文档，报 `File does not exist` | 顶层直接执行（同 `freecad-verify.py`） |
| 7 | freecadcmd 在 JSON 后追加上千行进度条 | `indexOf('{') + JSON.parse` 失败 | `lib/jsonout.mjs` 括号配平提取（正确跳过字符串与转义） |
| 8 | 产物 kind 正则 `/\.st[ep]$/` | 匹配不到 `.step`，STEP 全被标成 `report`，CAD 页看不到 STEP 卡 | `/\.(step\|stp)$/` |
| 9 | `result.json` **没有** `elapsed_ms`（只在 CLI stdout） | 面板耗时恒 `—` | `cloudforge.run` 落 `run.meta.json` 补齐 |
| 10 | 工作流只给布尔 `truth` | 客户端拼路径拼不对，verify 永远跑不起来 | 后端直接下发解析好的 `truthPath` |
| 11 | 场景无灯光 | Lambert 材质网格全黑 | `createStage` 里加 Ambient + 两盏 Directional |
| 12 | 叠加网格 38 个面同时半透明 | 点云被彻底盖住 | 未选中 opacity 0.06、选中 0.34；检测模式底云提到 0.45 |
| 13 | 注册到 `rightbar.session` | 面板**完全不出现**：宿主右栏 416px 里只有内置的"扩展面板/暂无指南"页签，第三方注册不产生渲染 | 改注册 `workbench.panel`（见 §1） |
| 14 | 直接 vendor 未压缩的 `three.core.js`（1,403,455 B） | 视口全白、只有 `three.core.js` 一个 404，其余资源 200 | 每个 client 资源 base64 后必须 ≤ 1 MiB，压缩后 381,420 B（见 §6） |
| 15 | 用 `element.style` / `setAttribute('style')` 传动态样式 | CSP 拦掉，每次一条 console error（首屏刷 87 条） | 离散值走 class/data-*，连续宽度走 `<progress>` 的 `value`/`max`（见 §6） |
| 16 | `source_cloud` 缺失时只给兜底选择器、不自动载入 | 面板打开是**空白视口**（0 draw call），而头部仍显示"196,133 点"，极具误导性 | 自动取最近候选并保留选择器；标签同时显示实际载入点数与检测输入点数 |
| 17 | `~/.agh` 的 ACL 被沙箱工具写入 `SU\CodexSandboxUsers` | daemon 报 `The credential store is unavailable`，浏览器侧 `config.get` 返 `SEMANTIC_REJECTED (-32011)`，工作区/模型/右栏全空 | `icacls "%USERPROFILE%\.agh" /remove:g SU\CodexSandboxUsers` 后重启 AGH |
| 18 | 用 PowerShell 的 `Get-Content`/`Set-Content` 改源码 | 它按 ANSI 读，UTF-8 中文被写成乱码（`收起` → `鏀惰捣`），且 `git checkout` 也救不回来 | 改文件一律用编辑工具；改完用 esbuild 解析一遍确认没坏 |
| 19 | 拖拽把手 `left:-4px` 放在 `overflow:hidden` 的面板里 | 外伸部分被裁掉，实际可命中区只剩一半 | 改成 `-4px` 起、`12px` 宽，落在面板内的条约 8px |
| 20 | 拖拽方向写成 `step + (当前x - 起点x)/48` | 往左拖得负值被 `clampStep` 夹在 0，表现为「拖不动」 | 位移取 `(起点x - 当前x)` 才是正数 |

## 9. 复现步骤

工具都在**本仓库** [`../agh-scripts/`](../agh-scripts/)，不依赖 agnes-harness 仓库的目录结构：
它们按 `AGH_ROOT`（或本仓库同级的 `../agnes-harness`）找到 AGH，再**动态 import** 官方 SDK。
上游仓库是 `AgnesAI-Labs/agnes-harness`，只读、不改。

```powershell
# 0) 若报 credential store is unavailable / SEMANTIC_REJECTED(-32011)：先修 ACL 再继续
icacls "$env:USERPROFILE\.agh" /remove:g SU\CodexSandboxUsers

# 0.5) 告诉脚本 AGH 在哪（若它不在本仓库同级目录才需要）
$env:AGH_ROOT = "E:/C++/Point/agnes-harness"

# 1) 装面板（CWD 必须是 CloudForge 仓库根：`file:` 源只接受 file:./ 相对路径）
cd E:\C++\Point\CloudForge
& E:\C++\Point\agnes-harness\node_modules\.bin\tsx.cmd hackathon\agh-scripts\install-package.mts "file:./hackathon/cloudforge-panel" "cloudforge-panel"

# 2) 名册自检：确认 entryUrl / slots / slotCatalogVersion 都在
& E:\C++\Point\agnes-harness\node_modules\.bin\tsx.cmd hackathon\agh-scripts\probe-roster.mts

# 3) 浏览器：硬刷新（Ctrl+Shift+R）后打开一个会话，右侧停靠列即出现控制台
#    再用另两个探针自检：
#      hackathon\agh-scripts\probe-roster.mts               # 名册：entryUrl / slots
#      hackathon\agh-scripts\probe-assets.mts <integrity>   # 逐资源：1 MiB 预算那关只有它看得见
#      hackathon\agh-scripts\probe-workspace.mts            # 工作区/profile/模型

# 4) 不依赖 AGH 的离线预览台（改前端时用，免去会话/工作区前置条件）
node hackathon/cloudforge-panel/tools/preview.mjs --port 5199
```

## 10. 离线预览台

`hackathon/cloudforge-panel/tools/preview.mjs` 起一个静态服务，加载**真实**的
`extensions/main/client/index.js` 与真实后端 handler，只做两处替换：

- `react` → 20 行的 `createElement` shim（`tools/react-shim.mjs`）
- `services.call` → 同源 `fetch('/svc/<name>')`，并**复刻 AGH 的 1 MiB 响应校验**

页面右侧用与真机同一套停靠列 CSS（`#main-content` 让位 + 绝对定位列 + `data-w` 分档），
所以宽度/折叠行为与真机一致。左侧诊断面板实时显示 `apply` 结果、槽注册、服务调用与 phase，
便于快速定位问题。

注意：预览台用的是普通浏览器 profile，而 AGH 右栏槽要求**活动会话 + 已信任工作区**。
用自动化浏览器打开 4177 时若报 `SEMANTIC_REJECTED (-32011)`，那是
`WORKSPACE_INVALID / not-found`（`local-dev` 的 `policy.workspacePackages: require-project-trust`
需要先信任工作区），与面板无关；在已配置好的工作台浏览器里打开即可。