
import plugin, { __fns } from './athena.mjs'
import { __atoms, host } from '@hermes/plugin-sdk'
import { __effects } from 'react'

// register() injects stylesheets, so a DOM has to exist for it to reach the
// part that registers panes. Minimal, but enough that a failure here means the
// plugin is wrong rather than the harness being incomplete.
const el = () => ({ textContent: '', style: {}, dataset: {}, remove() {}, append() {},
                    setAttribute() {}, addEventListener() {}, removeEventListener() {} })
globalThis.document = {
  createElement: el, head: { append() {} }, body: { append() {} },
  addEventListener() {}, removeEventListener() {}, querySelector: () => null,
}
globalThis.window = {
  addEventListener() {}, removeEventListener() {}, innerWidth: 1600, innerHeight: 900,
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}

let fail = 0
const ok = (label, cond, detail = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`)
  if (!cond) fail++
}
const guard = (label, fn) => {
  try { const v = fn(); ok(label, true); return v }
  catch (e) { ok(label, false, (e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : String(e))); return null }
}

ok('module loaded', !!plugin)
ok('id is athena', plugin.id === 'athena', String(plugin.id))

const registered = []
const restCalls = []
const ctx = {
  rest: async (p) => { restCalls.push(p); return {} },
  storage: { get: (k, d) => d, set: () => {} },
  os: {}, host: { notify() {}, revealPane() {} },
  i18n: { register(strings) { globalThis.__strings = strings } },
  register: (r) => { registered.push(r); return () => {} },
  onDispose: () => {},
}

guard('register(ctx) does not throw', () => plugin.register(ctx))
for (const r of registered) guard(`registered ${r.id} in area ${r.area}`, () => true)

// i18n dictionaries from both ported halves should have been merged
const en = globalThis.__strings && globalThis.__strings.en
ok('merged i18n strings registered', !!en && Object.keys(en).length > 0,
   en ? `${Object.keys(en).length} keys` : 'none')

// Walk a rendered tree, invoking function components for real. Recording which
// functions actually executed is what turns "it did not throw" into evidence
// that the ported halves really rendered — a tab that silently rendered nothing
// would otherwise pass just as quietly.
let nodes = 0
let seen = new Set()
// Components by name, so one can be re-invoked with props the tree cannot
// supply: SettingsTab takes the engine state as a prop, not from context.
const fns = new Map()
const walk = (node, depth) => {
  // A stub that passes `children` through hands back nested arrays, so flatten
  // before deciding whether this is even a node — otherwise whole subtrees
  // (the popover's body, and with it the compact card) are silently skipped.
  if (Array.isArray(node)) {
    for (const k of node) walk(k, depth)
    return
  }
  if (!node || typeof node !== 'object' || nodes > 4000 || depth > 10) return
  nodes++
  const t = node.type
  if (typeof t === 'function') {
    if (t.prototype && t.prototype.isReactComponent) return
    seen.add(t.name || '(anon)')
    fns.set(t.name || '(anon)', t)
    const out = t(node.props || {})
    walk(out, depth + 1)
    return
  }
  walk(node.props && node.props.children, depth + 1)
}

const panes = registered.filter(r => r.area === 'panes')
ok('two panes registered', panes.length === 2, `${panes.length}`)

// The tab atom is the one seeded with a tab name; use it to drive each tab.
const tabAtom = __atoms.find(a => a.__initial === 'apps')
ok('found the tab atom', !!tabAtom)

for (const p of panes) {
  guard(`render ${p.id} (apps tab)`, () => { nodes = 0; walk(p.render(), 0) })
}

// Which donor components each tab must actually reach, by name.
const expect = {
  files: ['Explorer'],           // filebox's explorer
  // AppsTab, not AppCard: the stubbed engine returns no apps, so the list has
  // nothing to draw a card for. Asserting the card here would fail on a healthy
  // plugin and prove nothing about the merge.
  apps: ['AppsTab'],
  // The Settings pane is a stack of collapsible sections now, so a plain tab
  // render reaches SettingsTab and Section. The row primitives live inside a
  // section body and are asserted below, driven with real engine state.
  settings: ['SettingsTab', 'Section'],
}
// No 'system': the standalone tab is gone. The deep monitor is a window opened
// from the mini monitor, and it is checked where the window API is on.
// No 'logs': it is not a tab any more. 'terminal' is rendered here with no app
// open, which is the empty-state branch — the bar never shows it that way.
for (const name of ['apps', 'files', 'store', 'terminal', 'settings', 'viewer', 'runs']) {
  guard(`render ${name} tab`, () => {
    tabAtom.set(name)
    nodes = 0
    seen = new Set()
    for (const p of panes) walk(p.render(), 0)
  })
  for (const want of (expect[name] || [])) {
    ok(`  ${name} tab executes ${want}`, seen.has(want),
       `ran: ${[...seen].slice(0, 14).join(', ')}`)
  }
}

// RunsTab only reaches its loaded branch through state, and the harness's hooks
// do not re-render, so the recorded run is asserted on RunsRow instead — the
// same component the tab draws, with the same data the backend sends.
const runsRow = __fns.RunsRow
const fakeRun = {
  id: 'RMBG-2-Studio.git/20261012-192005', app: 'RMBG-2-Studio.git', run: '20261012-192005',
  when: '20261012-192005', kind: 'gradio', capability: '/batch_process_images',
  intent: 'batch_process_images', inputs: ['C:/athena/in/me.png'],
  files: [{ name: 'me_nobg.png', path: 'C:/athena/runs/x/me_nobg.png', bytes: 8808, kind: 'image' },
          { name: 'stale.png', path: 'C:/athena/runs/x/stale.png', bytes: 4096, kind: 'image' }],
  likely_output: 'C:/athena/runs/x/me_nobg.png', dir: 'C:/athena/runs/x',
}
// Collect every string the tree renders, so a row that drew nothing can fail.
const strings = (node, out) => {
  if (Array.isArray(node)) { for (const k of node) strings(k, out); return out }
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (!node || typeof node !== 'object') return out
  if (typeof node.type === 'function') return strings(node.type(node.props || {}), out)
  return strings(node.props && node.props.children, out)
}
if (runsRow) {
  for (const isOpen of [false, true]) {
    let text = []
    guard(`render a recorded run (${isOpen ? 'expanded' : 'collapsed'})`, () => {
      nodes = 0; seen = new Set(); text = []
      strings(runsRow({ run: fakeRun, isOpen, thumbs: {}, onToggle: () => {} }), text)
    })
    const all = text.join(' | ')
    ok(`  the row names the app and what was asked for (${isOpen ? 'open' : 'closed'})`,
       all.includes('RMBG-2-Studio') && all.includes('batch process images'), all.slice(0, 120))
    if (isOpen) {
      ok('  an expanded run names the capability and flags its own output',
         all.includes('/batch_process_images') && all.includes('this run'),
         all.slice(0, 160))
      ok('  an expanded run offers the file and its size',
         all.includes('me_nobg.png') && all.includes('8.6 KB'), all.slice(0, 160))
    } else {
      ok('  a collapsed run stays collapsed', !all.includes('/batch_process_images'), all.slice(0, 120))
    }
  }
} else {
  ok('RunsRow is defined in the merge', false, 'not found')
}

// A run that is still going must not look finished, and one that failed has to
// say why: those are the two states a person reading the tab actually needs.
let liveText = []
guard('render a run that is still going', () => {
  nodes = 0; seen = new Set(); liveText = []
  strings(runsRow({ run: Object.assign({}, fakeRun, { state: 'running', files: [] }),
                    isOpen: true, thumbs: {}, onToggle: () => {} }), liveText)
})
ok('  a running run says it is running', liveText.join(' | ').includes('running'),
   liveText.join(' | ').slice(0, 100))
ok('  a running run does not read as finished',
   liveText.join(' | ').includes('still going'), liveText.join(' | ').slice(0, 140))
let failText = []
guard('render a run that failed', () => {
  nodes = 0; seen = new Set(); failText = []
  strings(runsRow({ run: Object.assign({}, fakeRun, { state: 'failed',
                    error: 'the app refused the file' }),
                    isOpen: true, thumbs: {}, onToggle: () => {} }), failText)
})
ok('  a failed run shows the reason', failText.join(' | ').includes('the app refused the file'),
   failText.join(' | ').slice(0, 140))

// SettingsTab takes the engine state as a prop, so it can be driven here with
// real data. Left undriven, every row that needs that state renders as the
// not-yet-loaded branch — the pass would look clean while proving nothing
// about the rows that matter.
const settingsFn = fns.get('SettingsTab')
if (settingsFn) {
  guard('render settings tab with engine state', () => {
    nodes = 0
    seen = new Set()
    walk(settingsFn({
      ctx,
      st: { ready: true, running: true, port: 42001, home: 'C:/athena', node: 'v22.21.1' },
      refresh: () => {},
    }), 0)
  })
  ok('  populated Settings reaches ListRow', seen.has('ListRow'),
     `ran: ${[...seen].slice(0, 16).join(', ')}`)
  ok('  populated Settings reaches StatusDot', seen.has('StatusDot'),
     `ran: ${[...seen].slice(0, 16).join(', ')}`)
  // The sections open by default where they must, so the primitives inside them
  // are reached on a plain render. A Section that swallowed its children would
  // leave the pane looking fine and empty.
  ok('  populated Settings reaches Section', seen.has('Section'),
     `ran: ${[...seen].slice(0, 16).join(', ')}`)
  ok('  populated Settings reaches ToggleRow', seen.has('ToggleRow'),
     `ran: ${[...seen].slice(0, 16).join(', ')}`)
}

// The status bar is its own render path and its own blast radius: a chip that
// throws takes the whole bar with it, and nothing above would have caught that.
const bars = registered.filter(r => r.area === 'right')
ok('status bar contributions registered', bars.length >= 2, `${bars.length}`)
let miniRan = null
for (const b of bars) {
  guard(`render status bar ${b.id}`, () => { nodes = 0; seen = new Set(); walk(b.render(), 0) })
  if (b.id === 'athena-minimon') miniRan = new Set(seen)
}
ok('  mini monitor renders (chip + compact card)',
   !!miniRan && miniRan.has('MiniMonitor') && miniRan.has('MiniCard'),
   miniRan ? `ran: ${[...miniRan].join(', ')}` : 'athena-minimon not registered')

// ---- the window switch ---------------------------------------------------
// Everything above ran with `host.openWorkspace` absent, which is why the
// plugin took its legacy path (two always-registered panes) and why the tab
// renders were reachable at all. That leaves the whole point of the chip
// untested: with the API present the panes must NOT be registered, the window
// must open once, and the chip must close and reopen it. Needs a fresh
// register, since the first one already took the panes.
plugin.dispose()
const windows = []
const closed = []
host.openWorkspace = (id, opts) => {
  windows.push({ id, opts })
  return () => { closed.push(id) }
}
registered.length = 0
guard('register(ctx) with host.openWorkspace does not throw', () => plugin.register(ctx))

const paneCount = registered.filter(r => r.area === 'panes').length
ok('  window API replaces the always-registered panes', paneCount === 0, `panes=${paneCount}`)
ok('  opens exactly one window', windows.length === 1, `windows=${windows.length}`)
ok('  window id and dock', windows[0] && windows[0].id === 'athena-window'
   && windows[0].opts && windows[0].opts.dock && windows[0].opts.dock.pos === 'right',
   windows[0] ? `${windows[0].id} ${JSON.stringify(windows[0].opts.dock)}` : 'none')
ok('  window renders the panel', typeof (windows[0] && windows[0].opts.render) === 'function')

// The chip is the switch. Reach the Button's handler through the same
// stub-passthrough walk the tab checks use.
const chipReg = registered.find(r => r.id === 'athena-chip')
let chipClick = null
const findClick = (node) => {
  if (Array.isArray(node)) { for (const k of node) findClick(k); return }
  if (!node || typeof node !== 'object') return
  if (node.props && typeof node.props.onClick === 'function') chipClick = node.props.onClick
  if (typeof node.type === 'function') findClick(node.type(node.props || {}))
  else findClick(node.props && node.props.children)
}
guard('render the chip with the window API present', () => { findClick(chipReg.render()) })
ok('  chip has a click handler', typeof chipClick === 'function')

guard('chip click #1 closes the window', () => chipClick())
ok('  window closed', closed.length === 1 && closed[0] === 'athena-window', JSON.stringify(closed))
guard('chip click #2 reopens the window', () => chipClick())
ok('  window reopened', windows.length === 2 && windows[1].id === 'athena-window',
   `windows=${windows.length}`)
guard('window render survives a close/reopen round trip', () => {
  nodes = 0; seen = new Set()
  walk(windows[1].opts.render(), 0)
})
ok('  reopened window renders the panel', seen.has('Panel') || seen.has('AppsTab'),
   `ran: ${[...seen].slice(0, 10).join(', ')}`)

// The deep monitor, opened from the mini monitor and closable like any other
// pane. The card only draws its footer once it has a reading, and the stub
// never lands one, so the handler is read off the MiniCard node as it is being
// built — the props are the wiring whether or not the body gets as far as
// drawing the button. Nothing else proves the merged half's panel is reachable:
// it is only in the tree through the factory the merge hands over.
const miniReg = registered.find(r => r.id === 'athena-minimon')
let deepOpen = null
const readMiniCard = (node) => {
  if (Array.isArray(node)) { for (const k of node) readMiniCard(k); return }
  if (!node || typeof node !== 'object') return
  const ty = node.type
  if (typeof ty === 'function') {
    if (ty.name === 'MiniCard' && node.props && 'onOpen' in node.props) deepOpen = node.props.onOpen
    readMiniCard(ty(node.props || {}))
  } else {
    readMiniCard(node.props && node.props.children)
  }
}
guard('render the mini monitor with the window API present',
      () => { readMiniCard(miniReg.render()) })
ok('  the mini monitor wires the deep view', typeof deepOpen === 'function')

guard('the deep view opens its own window', () => deepOpen())
ok('  system window opened', windows.some(w => w.id === 'athena-system'),
   `windows=${windows.map(w => w.id).join(', ')}`)
const sysWin = windows.find(w => w.id === 'athena-system')
guard('the system window renders the deep view', () => {
  nodes = 0; seen = new Set()
  if (sysWin) walk(sysWin.opts.render(), 0)
})
ok('  system window reaches the ported panel', seen.has('Meter'),
   `ran: ${[...seen].slice(0, 14).join(', ')}`)
ok('  system window reaches the ranked consumers', seen.has('TopConsumers'),
   `ran: ${[...seen].slice(0, 14).join(', ')}`)
ok('  and the shelf that says what the disk holds', seen.has('WeightShelf'),
   `ran: ${[...seen].slice(0, 16).join(', ')}`)

// ---- a reopen paints from the memo --------------------------------------
// The reported symptom: close the window, open it again, and "Loading apps…"
// (then "The engine is stopped.") sits over data that never changed. Host
// teardown rebuilds this whole tree, so the tab now seeds itself from a
// module-level memo. Both halves have to hold: a genuinely cold load still
// shows the placeholder, and a warm one shows the apps.
const texts = (node, out = []) => {
  if (Array.isArray(node)) { for (const k of node) texts(k, out); return out }
  if (typeof node === 'string') { out.push(node); return out }
  if (!node || typeof node !== 'object') return out
  const t = node.type
  if (typeof t === 'function') { texts(t(node.props || {}), out); return out }
  return texts(node.props && node.props.children, out)
}
const hasText = (arr, needle) => arr.some(s => typeof s === 'string' && s.indexOf(needle) !== -1)

ctx.rest = async (p) => {
  restCalls.push(p)
  if (p === '/apps') return { apps: [{ id: 'wan2gp', name: 'WAN2GP' }] }
  if (p === '/status') return { ready: true, running: true, port: 42001, home: 'C:/athena' }
  return {}
}

// Forget effects queued by the earlier walks so only this mount's land below.
__effects.splice(0)
tabAtom.set('apps')
let cold = []
guard('cold open renders the loading placeholder', () => {
  nodes = 0; seen = new Set()
  const tree = windows[1].opts.render()
  cold = texts(tree)
  walk(tree, 0)
})
ok('  cold really is cold (placeholder shown)', hasText(cold, 'Loading apps'),
   cold.filter(s => typeof s === 'string').slice(0, 5).join(' | ') || '(no text at all)')

// Run the mount effects so /apps lands and the memo fills. useState's setter is
// a no-op in this stub, so the memo is the ONLY thing that can make the next
// render right — which is exactly the property under test.
for (const f of __effects.splice(0)) { guard('  mount effect runs', () => f()) }
await new Promise(r => setTimeout(r, 50))

let warm = []
guard('warm reopen renders the panel', () => {
  nodes = 0; seen = new Set()
  const tree = windows[1].opts.render()
  warm = texts(tree)
  walk(tree, 0)
})
ok('  warm reopen shows no loading placeholder', !hasText(warm, 'Loading apps'),
   warm.filter(s => typeof s === 'string').slice(0, 6).join(' | ') || '(no text at all)')
ok('  warm reopen is not the engine-stopped screen', !hasText(warm, 'engine is stopped'),
   warm.filter(s => typeof s === 'string').slice(0, 6).join(' | ') || '(no text at all)')
ok('  warm reopen draws the app card', seen.has('AppCard'),
   `ran: ${[...seen].slice(0, 14).join(', ')}`)


// ---- the terminal is a tab you open, not furniture ----------------------
// It used to be a standing Logs tab: always in the bar, whether or not there
// was anything to watch. Now it is opened by the app card's own button, which
// means the bar itself is the thing to assert -- absent by default, present
// while an app's terminal is open, gone again when it is closed.
const barOf = (node) => {
  if (Array.isArray(node)) { for (const k of node) { const r = barOf(k); if (r) return r } return null }
  if (!node || typeof node !== 'object') return null
  const kids = node.props && node.props.children
  const arr = Array.isArray(kids) ? kids : []
  // Two bars are called ah-tabs (the store's sub-tabs are the other), so pick
  // the one whose first button is the Apps tab.
  if (node.props && node.props.className === 'ah-tabs'
      && arr.some(b => b && b.props && b.props.children === 'Apps')) return node
  if (typeof node.type === 'function') return barOf(node.type(node.props || {}))
  return barOf(kids)
}
const barLabels = (tree) => {
  const bar = barOf(tree)
  if (!bar) return null
  const kids = bar.props.children
  return (Array.isArray(kids) ? kids : [kids])
    .map(b => (b && b.props ? b.props.children : null))
    .filter(x => typeof x === 'string')
}

tabAtom.set('apps')
let closedBar = []
guard('render the bar with no terminal open', () => { closedBar = barLabels(windows[1].opts.render()) || [] })
ok('  the bar renders at all', closedBar.length >= 6, JSON.stringify(closedBar))
ok('  there is no Logs tab', !closedBar.includes('Logs'), JSON.stringify(closedBar))
ok('  there is no Terminal tab before anything asks for one',
   !closedBar.includes('Terminal'), JSON.stringify(closedBar))

// Which values put it there? Every atom that starts null is offered to the bar.
// There are exactly two kinds of "there is output to watch": the app whose
// terminal is open, and a job with output and no app behind it — updating the
// whole shelf is one. Anything beyond those two would mean the tab is standing
// furniture behind some unrelated value.
const nullAtoms = __atoms.filter(a => a.__initial === null)
const openers = []
for (const a of nullAtoms) {
  a.set('RMBG-2-Studio.git')
  const labels = barLabels(windows[1].opts.render()) || []
  if (labels.includes('Terminal')) openers.push({ atom: a, labels })
  a.set(null)
}
ok('  only the two values that mean "there is output to watch" open the Terminal tab',
   openers.length === 2,
   `${openers.length} of ${nullAtoms.length} atoms did: ${openers.map(o => JSON.stringify(o.labels)).join(' ')}`)
const termAtom = openers.length >= 1 ? openers[0].atom : null
if (termAtom) {
  const labels = openers[0].labels
  ok('  it sits with the app views, in front of Runs',
     labels.indexOf('Terminal') < labels.indexOf('Runs') && labels.indexOf('Terminal') > 0,
     JSON.stringify(labels))
  ok('  Settings keeps the last slot', labels[labels.length - 1] === 'Settings', JSON.stringify(labels))
  termAtom.set('RMBG-2-Studio.git')
  const again = barLabels(windows[1].opts.render()) || []
  ok('  and closing it puts the bar back', (() => {
    termAtom.set(null)
    const back = barLabels(windows[1].opts.render()) || []
    return JSON.stringify(back) === JSON.stringify(closedBar)
  })(), JSON.stringify(again))
}

// The other opener is a job with no app: an action that belongs to the whole
// shelf. Its output is the whole screen, so the terminal opens with no app and
// shows the job alone — that is the case the title has to cover.
if (openers.length === 2) {
  const jobAtom = openers[1].atom
  guard('a shelf job opens the terminal too', () => {
    jobAtom.set({ app: null, id: 'job-shelf' })
    tabAtom.set('terminal')
  })
  const labels = barLabels(windows[1].opts.render()) || []
  ok('  the bar has the tab for a job with no app', labels.includes('Terminal'), JSON.stringify(labels))
  guard('render it with no app open', () => {
    nodes = 0; seen = new Set()
    walk(windows[1].opts.render(), 0)
  })
  ok('  and the screen is the terminal, not a demand for an app', seen.has('TerminalTab'),
     `ran: ${[...seen].slice(0, 12).join(', ')}`)
  guard('close the shelf job', () => { jobAtom.set(null); tabAtom.set('apps') })
}

// The app card's own button is the way in: click it and the tab appears.
let termClick = null
const findTermButton = (node) => {
  if (Array.isArray(node)) { for (const k of node) findTermButton(k); return }
  if (!node || typeof node !== 'object') return
  // Read the props before invoking the component: the SDK's Button is a stub
  // here, and its onClick only exists on the node being built.
  if (!termClick && node.props && node.props.children === 'Terminal'
      && typeof node.props.onClick === 'function') termClick = node.props.onClick
  if (typeof node.type === 'function') findTermButton(node.type(node.props || {}))
  else findTermButton(node.props && node.props.children)
}
guard('render an app card', () => {
  findTermButton(__fns.AppCard({ ctx, app: { id: 'RMBG-2-Studio.git', name: 'RMBG' }, onChange: () => {} }))
})
ok('  the card offers a Terminal button, not a Logs one', typeof termClick === 'function')
if (typeof termClick === 'function' && termAtom) {
  tabAtom.set('apps')
  guard('click the card\'s Terminal button', () => termClick())
  const after = barLabels(windows[1].opts.render()) || []
  ok('  clicking it opens the tab', after.includes('Terminal'), JSON.stringify(after))
  ok('  and the body is the terminal', (seen = new Set(),
      walk(windows[1].opts.render(), 0), seen.has('TerminalTab')),
     `ran: ${[...seen].slice(0, 12).join(', ')}`)
}

// ---- is there anything to update to? --------------------------------------
// The question a person has is about the whole shelf, not about one card. The
// bar is drawn only when an answer says something is behind — and its button is
// what starts the work, as one job watched on a terminal that has no app.
const updRoutes = []
const prevRest = ctx.rest
ctx.rest = async (p, opts) => {
  updRoutes.push(((opts && opts.method) || 'GET') + ' ' + p)
  if (p.indexOf('/apps/updates') === 0) {
    return { ok: true, any: true, checked: 2,
             updates: ['wan.git', 'comfy.git'], behind: { 'wan.git': 3, 'comfy.git': 1 } }
  }
  if (p === '/apps') {
    return { apps: [{ id: 'wan.git', name: 'WAN2GP' }, { id: 'comfy.git', name: 'ComfyUI' }] }
  }
  if (p === '/status') return { ready: true, running: true }
  if (p.indexOf('/apps/update-all') === 0) {
    return { ok: true, job: 'job-upd-1', apps: ['wan.git', 'comfy.git'] }
  }
  if (p.indexOf('/jobs/') === 0) {
    // Running first, then done: the landing this block is about only means
    // something if the panel actually spent a tick on the job's own tab. A job
    // that reports done immediately can be landed back from without ever having
    // gone anywhere.
    jobPolls += 1
    if (jobPolls === 1) return { state: 'running', lines: ['updating wan.git'] }
    return { state: 'done', result: { updated: ['wan.git', 'comfy.git'], failed: [] } }
  }
  return {}
}
let jobPolls = 0
__effects.splice(0)
tabAtom.set('apps')
let firstPaint = []
guard('apps tab, first paint', () => { firstPaint = texts(windows[1].opts.render()) })
ok('  nothing claims there are updates before anything has been checked',
   !hasText(firstPaint, 'Update all'),
   firstPaint.filter(s => typeof s === 'string').slice(0, 6).join(' | ') || '(no text)')
for (const f of __effects.splice(0)) { guard('  updates effect runs', () => f()) }
await new Promise(r => setTimeout(r, 60))
ok('  the check asked every app at once', updRoutes.some(r => r.indexOf('GET /apps/updates') === 0),
   JSON.stringify(updRoutes))
let barPaint = []
guard('paint again, now that an answer is in', () => { barPaint = texts(windows[1].opts.render()) })
ok('  the shelf says how many apps have updates', hasText(barPaint, '2 apps have updates'),
   barPaint.filter(s => typeof s === 'string').slice(0, 8).join(' | ') || '(no text)')

let updClick = null
const findLabel = (node, label) => {
  if (Array.isArray(node)) { for (const k of node) findLabel(k, label); return }
  if (!node || typeof node !== 'object') return
  if (!updClick && node.props && node.props.children === label
      && typeof node.props.onClick === 'function') updClick = node.props.onClick
  if (typeof node.type === 'function') findLabel(node.type(node.props || {}), label)
  else findLabel(node.props && node.props.children, label)
}
guard('render the bar to find its button', () => findLabel(windows[1].opts.render(), 'Update all'))
ok('  the bar offers one button that updates them all', typeof updClick === 'function')
if (typeof updClick === 'function') {
  tabAtom.set('apps')
  guard('press Update all', () => updClick())
  await new Promise(r => setTimeout(r, 120))
  const at = updRoutes.indexOf('POST /apps/update-all')
  ok('  it starts the update as a job, not as a request that hangs', at >= 0,
     JSON.stringify(updRoutes))
  ok('  and then watches that job', at >= 0
     && updRoutes.slice(at + 1).some(r => r.indexOf('GET /jobs/') === 0),
     JSON.stringify(updRoutes.slice(Math.max(0, at))))
  ok('  nothing was asked of GitHub by the panel itself',
     !updRoutes.some(r => /github|push|publish/i.test(r)), JSON.stringify(updRoutes))
  // A shelf-wide update takes minutes, so the panel moves to the terminal tab
  // while it runs; the question is where it is when the job ends. It used to stay
  // there, on a tab that only exists while a job does: the tab was gone from the
  // bar and the screen showed a tab nobody could see. The answer to "did it
  // update" is on the shelf, so that is where both the note and the reader go.
  ok('  while the job runs the panel is watching it on the terminal tab',
     tabAtom.get() === 'terminal', String(tabAtom.get()))
  await new Promise(r => setTimeout(r, 1800))          // one poll interval, plus slack
  ok('  and when it ends it comes back to the shelf, not to a tab that is gone',
     tabAtom.get() === 'apps', String(tabAtom.get()))
  const doneAt = updRoutes.lastIndexOf('GET /jobs/')
  ok('  and it asks again whether anything is still behind, so the bar can go away',
     updRoutes.slice(doneAt + 1).some(r => r.indexOf('GET /apps/updates') === 0),
     JSON.stringify(updRoutes.slice(Math.max(0, doneAt))))
  ok('  and re-reads the shelf it just changed',
     updRoutes.slice(doneAt + 1).indexOf('GET /apps') >= 0,
     JSON.stringify(updRoutes.slice(Math.max(0, doneAt))))
}
ctx.rest = prevRest

// ---- what the shelf says when a shelf-wide update finishes --------------------
// One sentence is the whole account a person gets without opening a terminal, so
// it is checked as words rather than left inside a handler whose state the suite
// cannot see: how many moved, and — when some did not — that the reasons are
// somewhere, rather than a bare number that reads like a total.
{
  const words = __fns.updateAllWords
  ok('  a clean run counts what moved', words(2, 0) === '2 updated', String(words(2, 0)))
  ok('  one app reads as one app, not as a plural', words(1, 0) === '1 updated', String(words(1, 0)))
  ok('  a run that changed nothing says so rather than "0 updated"',
     words(0, 0) === 'nothing needed updating', String(words(0, 0)))
  ok('  a partial failure keeps both numbers and says where the reasons are',
     /2 updated/.test(words(2, 1)) && /1 did not/.test(words(2, 1))
     && /Terminal/.test(words(2, 1)), String(words(2, 1)))
}

// ---- publishing announces itself before it pushes -------------------------
// Publishing is the one thing on an app card that leaves this machine, so the
// order matters: ask, then post, then watch the job. A click that reaches GitHub
// without the first step is the bug this guards, and it is guarded by driving
// the button's own handler rather than by reading the source.
const pubRoutes = []
let pubAsk = ''
const pubCtx = {
  ...ctx,
  rest: async (p, opts) => {
    pubRoutes.push(((opts && opts.method) || 'GET') + ' ' + p)
    if (p.indexOf('/publish') > 0) {
      return { ok: true, job: 'PUB1', repo: 'owner/athena-store',
               branch: 'athena-rmbg-2-studio', store: 'https://github.com/owner/athena-store' }
    }
    if (p.indexOf('/jobs/') === 0) return { ok: true, state: 'done', tail: 'Pushed 4 branches\n' }
    return {}
  },
}
const realConfirm = globalThis.window.confirm
let pubClick = null
let pubTree = null
const findPublishButton = (node) => {
  if (Array.isArray(node)) { for (const k of node) findPublishButton(k); return }
  if (!node || typeof node !== 'object') return
  if (!pubClick && node.props && node.props.children === 'Publish'
      && typeof node.props.onClick === 'function') pubClick = node.props.onClick
  if (typeof node.type === 'function') findPublishButton(node.type(node.props || {}))
  else findPublishButton(node.props && node.props.children)
}
const pubApp = { id: 'RMBG-2-Studio.git', name: 'RMBG', description: 'cutouts', tags: [] }
guard('render an app card for the publish check', () => {
  // Publish lives in the card's menu now, so the check opens it: the same card,
  // the same handlers, one click further in. `menuOpen` is what a person does with
  // the ⋯ — the harness's hooks cannot flip state, so the state is handed in.
  pubTree = __fns.AppCard({ ctx: pubCtx, app: pubApp, onChange: async () => {}, menuOpen: true })
  findPublishButton(pubTree)
})
ok('  the card offers Publish', typeof pubClick === 'function')

guard('click Publish, and answer yes', () => {
  globalThis.window.confirm = (q) => { pubAsk = String(q); return true }
  return pubClick ? pubClick() : null
})
// The handler is async, so let its awaits drain before reading what it did.
await new Promise((r) => setTimeout(r, 50))
ok('  it asked first, and named what it was about to do',
   /Publish RMBG to the store/.test(pubAsk), pubAsk.split('\n')[0])
ok('  it posted to that app\'s publish route',
   pubRoutes.includes('POST /apps/RMBG-2-Studio.git/publish'), JSON.stringify(pubRoutes))
ok('  it watched the job it was given',
   pubRoutes.some((r) => r === 'GET /jobs/PUB1'), JSON.stringify(pubRoutes))

// And the other half: saying no must mean nothing is sent, because a store that
// pushes on a declined click is worse than one that never publishes at all.
pubRoutes.length = 0
let pubClick2 = null
guard('render the card again', () => {
  const find2 = (node) => {
    if (Array.isArray(node)) { for (const k of node) find2(k); return }
    if (!node || typeof node !== 'object') return
    if (!pubClick2 && node.props && node.props.children === 'Publish'
        && typeof node.props.onClick === 'function') pubClick2 = node.props.onClick
    if (typeof node.type === 'function') find2(node.type(node.props || {}))
    else find2(node.props && node.props.children)
  }
  find2(__fns.AppCard({ ctx: pubCtx, app: pubApp, onChange: async () => {} }))
})
guard('click Publish, and answer no', () => {
  globalThis.window.confirm = () => false
  return pubClick2 ? pubClick2() : null
})
await new Promise((r) => setTimeout(r, 20))
ok('  a declined publish sends nothing at all', pubRoutes.length === 0,
   JSON.stringify(pubRoutes))
globalThis.window.confirm = realConfirm

// ---- the store's install outlives the card, and its terminal follows -----
// An install is minutes of cloning and building, and moving to another tab
// unmounts the entire store. The record lives at module scope, so a card opened
// after the job started shows the work it missed — the difference between
// "still installing" and a card that has forgotten it ever began. `__fns` is
// the module's own surface: none of this is re-implemented for the test.
const TERM = 'cloning the repository\nbuilding wheels step 2 of 11\nready\n'
const instRoutes = []
const instCtx = {
  ...ctx,
  rest: async (p) => {
    instRoutes.push(p)
    if (p.indexOf('/catalog/install') === 0) return { ok: true, job: 'J1' }
    if (p.indexOf('/jobs/') === 0) {
      return { ok: true, state: 'done', tail: TERM, progress: { done: 11, total: 11 } }
    }
    return {}
  },
}
const cardFor = (slug) => __fns.StoreCard({ ctx: instCtx, app: { slug, name: slug } })

// Mid-flight: the state in the report — building, output streaming.
__fns.installWrite('live', { state: 'building', text: 'step 2 of 11\n',
                             progress: { done: 2, total: 11 } })
let midTexts = []
guard('a card opened mid-install renders it', () => { midTexts = texts(cardFor('live')) })
ok('  the terminal is redrawn from the registry', hasText(midTexts, 'step 2 of 11'),
   midTexts.filter(s => typeof s === 'string').slice(0, 4).join(' | '))
ok('  the button says it is still building', hasText(midTexts, 'Building\u2026'),
   midTexts.filter(s => typeof s === 'string').slice(0, 6).join(' | '))

let otherTexts = []
guard('another card is unaffected', () => { otherTexts = texts(cardFor('demo')) })
ok('  no leakage between cards', !hasText(otherTexts, 'step 2 of 11'))

// The whole job runs with no component mounted at all.
// guard() is synchronous — it returns whatever the body returned, so the
// promise comes back here and is awaited below. Awaiting it inside guard would
// let every assertion after it run against a job that had not started yet.
const started = guard('start an install with nothing mounted',
                      () => __fns.startInstall(instCtx, 'demo', 'demo', 'Demo'))
const finished = await started
ok('  the install completes unmounted', finished === true, String(finished))
ok('  it polled the job by id', instRoutes.some(p => p.indexOf('/jobs/J1') === 0),
   instRoutes.slice(-3).join(', '))
const rec = __fns.installRead('demo')
ok('  the record holds the final state', !!rec && rec.state === 'installed',
   rec ? rec.state : 'no record')
ok('  the record holds the tail', !!rec && rec.text === TERM,
   rec ? JSON.stringify(rec.text.slice(0, 32)) : '')

let doneTexts = []
guard('the card reopened afterwards shows the finished install',
      () => { doneTexts = texts(cardFor('demo')) })
ok('  the output is still on the card', hasText(doneTexts, 'building wheels step 2 of 11'),
   doneTexts.filter(s => typeof s === 'string').slice(0, 4).join(' | '))
ok('  the badge replaced the install button', hasText(doneTexts, 'installed'),
   doneTexts.filter(s => typeof s === 'string').slice(0, 6).join(' | '))

// A failure keeps its output too: the reason is almost always in it.
__fns.installWrite('broken', { state: 'failed', text: 'ModuleNotFoundError: torch\n',
                              note: 'install failed' })
let badTexts = []
guard('a failed card keeps its output', () => { badTexts = texts(cardFor('broken')) })
ok('  the failure text survives', hasText(badTexts, 'ModuleNotFoundError'),
   badTexts.filter(s => typeof s === 'string').slice(0, 4).join(' | '))
ok('  the failure offers a retry', hasText(badTexts, 'Retry'),
   badTexts.filter(s => typeof s === 'string').slice(0, 6).join(' | '))

// ---- the terminal follows its own output ---------------------------------
// Both halves matter and neither is visible in a still frame: the box has to
// move to the end as output arrives, and it has to stop the moment someone
// scrolls up to read something, or a ten-minute build fights the reader.
__effects.splice(0)
const termNode = __fns.AutoTerm({ text: 'first line\n' })
ok('the terminal is a scrollable box', !!termNode && termNode.type === 'pre',
   termNode ? String(termNode.type) : 'nothing rendered')
ok('  it keeps a handle on that box', !!termNode.props.ref && 'current' in termNode.props.ref)
ok('  it watches for the reader scrolling away',
   typeof termNode.props.onScroll === 'function')
ok('  it renders the output', termNode.props.children === 'first line\n',
   JSON.stringify(termNode.props.children))

const box = { scrollHeight: 500, scrollTop: 0, clientHeight: 100 }
termNode.props.ref.current = box
const followEffect = __effects.splice(0)[0]
guard('the follow effect runs', () => followEffect())
ok('  new output jumps the box to the end', box.scrollTop === 500, String(box.scrollTop))

// The reader scrolls up to read something: following has to stop.
termNode.props.onScroll({ currentTarget: { scrollHeight: 500, scrollTop: 0, clientHeight: 100 } })
box.scrollTop = 12
guard('the follow effect runs again with new output', () => followEffect())
ok('  it stays where the reader left it', box.scrollTop === 12, String(box.scrollTop))

// Back at the bottom, it follows again.
termNode.props.onScroll({ currentTarget: { scrollHeight: 500, scrollTop: 380, clientHeight: 100 } })
box.scrollTop = 380
guard('and again', () => followEffect())
ok('  it follows again once back at the bottom', box.scrollTop === 500, String(box.scrollTop))

// ---- a long list needs a way in, and a running app needs a way out -------
// Two things the apps tab could not do: tell you which apps are up right now
// without scrolling for a green badge, and find one in a list that grows every
// time you try something new. Both are driven here with real payload shapes.
const six = [
  { id: 'RMBG-2-Studio.git', name: 'RMBG 2 Studio', description: 'cutout', tags: ['image'],
    running: true, url: 'http://127.0.0.1:7860' },
  { id: 'wan2gp.git', name: 'WAN2GP', description: 'video generation', tags: ['video'],
    running: true, url: 'http://127.0.0.1:7861' },
  { id: 'comfy.git', name: 'ComfyUI', description: 'node graphs', tags: ['image'] },
  { id: 'datasette', name: 'Datasette', description: 'browse data' },
  { id: 'Maestro.git', name: 'Maestro', description: 'orchestrate' },
  { id: 'Hunyuan3d-2-lowvram.git', name: 'Hunyuan3D', description: 'meshes', tags: ['3d'] },
]

// The filter itself, with the arguments the panel passes.
ok('appMatches lets everything through when nothing is typed',
   six.every(a => __fns.appMatches(a, '')) && __fns.appMatches(six[0], '   '))
ok('  it matches a name', __fns.appMatches(six[1], 'wan2gp'))
ok('  it matches a folder id', __fns.appMatches(six[0], 'rmbg-2-studio.git'))
ok('  it ignores case', __fns.appMatches(six[2], 'COMFYUI'))
ok('  it matches a description', __fns.appMatches(six[5], 'meshes'))
ok('  it matches a tag', __fns.appMatches(six[5], '3d'))
ok('  and it does not match what is not there', !__fns.appMatches(six[3], 'torch'))

// Six apps, two of them up. The tab renders from its memo, so this drives the
// same two steps the panel does: mount (which queues the load) then the effect.
ctx.rest = async (p) => {
  if (p === '/apps') return { apps: six }
  if (p === '/status') return { ready: true, running: true, port: 42001, home: 'C:/athena' }
  return {}
}
__effects.splice(0)
guard('mount the tab on a long list', () => { __fns.AppsTab(
  { ctx, st: { ready: true, running: true, home: 'C:/athena' }, refresh: () => {} }) })
for (const f of __effects.splice(0)) { await f() }
await new Promise(r => setTimeout(r, 50))

let listTexts = []
let listTree = null
guard('the apps tab renders a long list', () => {
  listTree = __fns.AppsTab({ ctx, st: { ready: true, running: true, home: 'C:/athena' }, refresh: () => {} })
  listTexts = texts(listTree)
})
ok('  it says how many are up', hasText(listTexts, '2 running now'),
   listTexts.filter(s => typeof s === 'string').slice(0, 8).join(' | '))
ok('  the running section names the apps',
   hasText(listTexts, 'RMBG 2 Studio') && hasText(listTexts, 'WAN2GP'))
ok('  it offers a way out of each', hasText(listTexts, 'Stop'))
ok('  it offers a way back in', hasText(listTexts, 'Open'))
// The search box is an input: its words live in props, not in a text node.
const findNode = (node, pred, hit = []) => {
  if (Array.isArray(node)) { for (const k of node) findNode(k, pred, hit); return hit }
  if (!node || typeof node !== 'object') return hit
  const t = node.type
  if (typeof t === 'function') { findNode(t(node.props || {}), pred, hit); return hit }
  if (pred(node)) hit.push(node)
  return findNode(node.props && node.props.children, pred, hit)
}
const searchBox = findNode(listTree, n => n.props && n.props.type === 'search')
ok('  and a way to find an app at all', searchBox.length === 1,
   `found ${searchBox.length} search inputs`)
ok('  the box says what it searches',
   searchBox[0] && String(searchBox[0].props.placeholder).includes('name, id or tag'),
   searchBox[0] ? String(searchBox[0].props.placeholder) : '')
ok('  typing into it filters what is drawn',
   searchBox[0] && typeof searchBox[0].props.onChange === 'function')

// The same tab on a short list, with nothing running: it has to drop both
// pieces of furniture. The memo is what it renders from, so this drives the
// engine stub and the mount effect the way the panel does, then re-renders.
ctx.rest = async (p) => {
  if (p === '/apps') {
    return { apps: [
      { id: 'comfy.git', name: 'ComfyUI' },
      { id: 'datasette', name: 'Datasette' },
      { id: 'Maestro.git', name: 'Maestro' },
    ] }
  }
  if (p === '/status') return { ready: true, running: true, port: 42001, home: 'C:/athena' }
  return {}
}
__effects.splice(0)
guard('mount the tab on a short list', () => { __fns.AppsTab(
  { ctx, st: { ready: true, running: true, home: 'C:/athena' }, refresh: () => {} }) })
for (const f of __effects.splice(0)) { await f() }
await new Promise(r => setTimeout(r, 50))

let shortTexts = []
guard('a short list renders without the furniture', () => { shortTexts = texts(__fns.AppsTab(
  { ctx, st: { ready: true, running: true, home: 'C:/athena' }, refresh: () => {} })) })
ok('  no running section when nothing is running', !hasText(shortTexts, 'running now'),
   shortTexts.filter(s => typeof s === 'string').slice(0, 6).join(' | '))
ok('  no search box on a list you can see at once',
   !hasText(shortTexts, 'Find an app by name'),
   shortTexts.filter(s => typeof s === 'string').slice(0, 6).join(' | '))
ok('  but the apps are all there',
   hasText(shortTexts, 'ComfyUI') && hasText(shortTexts, 'Maestro'))

// ---- a check that knows its fix offers it ----------------------------------
// "This app cannot generate" is not breakage — a fresh checkout ships an empty
// models/ while the machine's weights live in one folder somewhere else — so it
// must not be dressed as breakage. It is still the one thing standing between
// the app and its job, and the plugin already knows which folders on this
// machine would end it: the row carries the step, and the button asks for those
// folders instead of asking a person to type a path.
const modelCalls = []
const healthRest = ctx.rest
ctx.rest = async (p, opts) => {
  const method = (opts && opts.method) || 'GET'
  modelCalls.push(method + ' ' + p + (opts && opts.body ? ' ' + JSON.stringify(opts.body) : ''))
  if (p.indexOf('/models') > 0) {
    if (method === 'POST') {
      return { ok: true, path: 'C:/athena/api/comfy.git/app/extra_model_paths.yaml',
               base_path: 'E:/AI MODELS/models', models: { ready: true, count: 16 } }
    }
    return { ok: true, models: { ready: false, count: 0, next_step: null },
             candidates: [{ folder: 'E:/AI MODELS/models', total: 16, apps: ['comfy'] }] }
  }
  return {}
}
const stepHealth = { checks: [
  { name: 'Structure', ok: true, detail: 'everything is where it should be' },
  { name: 'Weights', ok: false, informational: true, detail: 'no model weights present',
    next_step: { action: 'point_models', label: 'point ComfyUI at a weights folder' } },
] }
const findPress = (node, want) => {
  if (Array.isArray(node)) {
    for (const k of node) { const hit = findPress(k, want); if (hit) return hit }
    return null
  }
  if (!node || typeof node !== 'object') return null
  const kids = node.props && node.props.children
  if (typeof kids === 'string' && kids.indexOf(want) === 0
      && typeof node.props.onClick === 'function') return node.props.onClick
  if (typeof node.type === 'function') return findPress(node.type(node.props || {}), want)
  return findPress(kids, want)
}
let changed = 0
let stepTree = []
guard('render a health report whose failing check knows its fix', () => {
  stepTree = __fns.HealthReport({ health: stepHealth, app: { id: 'comfy.git' }, ctx,
                                  onChange: () => { changed++ } })
})
const stepTexts = texts(stepTree)
ok('  the check that is missing weights reads as a note, not as breakage',
   hasText(stepTexts, 'note') || !hasText(stepTexts, 'broken'), stepTexts.join(' | '))
// The reason rides in the row's tooltip rather than in the row: these lines are
// one short line each, and a wrapped paragraph per check would bury the list.
const titles = (node, out = []) => {
  if (Array.isArray(node)) { for (const k of node) titles(k, out); return out }
  if (!node || typeof node !== 'object') return out
  if (typeof (node.props && node.props.title) === 'string') out.push(node.props.title)
  if (typeof node.type === 'function') titles(node.type(node.props || {}), out)
  else titles(node.props && node.props.children, out)
  return out
}
ok('  and the reason is still there, in the row it belongs to',
   titles(stepTree).some(t => t.indexOf('no model weights') !== -1), JSON.stringify(titles(stepTree)))
const press = findPress(stepTree, 'point ComfyUI')
ok('  the step is a button, not just a sentence', typeof press === 'function')
const plain = __fns.HealthReport({ health: { checks: [{ name: 'Structure', ok: true }] },
                                   app: { id: 'comfy.git' }, ctx, onChange: () => {} })
ok('  a check with no step gets no button', !findPress(plain, 'point'))

if (typeof press === 'function') {
  guard('press the step button', () => press())
  await new Promise(r => setTimeout(r, 60))
  ok('  it asked the plugin where this machine keeps weights, instead of for a path',
     modelCalls.some(c => c.indexOf('GET /apps/comfy.git/models') === 0), JSON.stringify(modelCalls))
  ok('  and pointed the app at that folder in the same gesture',
     modelCalls.some(c => c.indexOf('POST /apps/comfy.git/models') === 0
                          && c.indexOf('E:/AI MODELS/models') > 0), JSON.stringify(modelCalls))
  ok('  without replacing a mapping that is already there unless told to',
     modelCalls.every(c => c.indexOf('POST') !== 0 || c.indexOf('"force":false') > 0),
     JSON.stringify(modelCalls))
  ok('  and the card refreshed, so the row it fixed redraws', changed > 0, `onChange ran ${changed}x`)
}

// ---- why it stopped, when the log says --------------------------------------
// The block that sits above the checks: the failing program's own sentence,
// quoted, and the one action that answers it. The action matters more than the
// copy does — this panel used to run the weights flow for every button on it, so
// a card being held by another app (and every other non-weight cause) was offered
// the wrong fix, which is the defect these checks hold down.
const failCalls = []
ctx.rest = async (p, opts) => {
  failCalls.push(((opts && opts.method) || 'GET') + ' ' + p)
  return { ok: true, written: ['cache/HF_AUTH/token'] }
}
const failHealth = {
  checks: [{ name: 'Structure', ok: true, detail: 'everything is where it should be' }],
  failure: {
    cause: 'cuda_oom',
    title: 'The graphics card ran out of memory',
    why: 'torch asked for 111.76 GiB of a card that has 31.82 GiB, while wan.git was '
       + 'holding most of it.',
    evidence: { line: 4, text: 'torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 111.76 GiB.' },
    also: [{ cause: 'out_of_ram', line: 9 }],
    fix: { action: 'stop_app', label: 'Stop wan.git and try again', apps: ['wan.git'],
           detail: 'the card is being held by another app',
           // What triage actually produces for a held card: both answers, because
           // closing the neighbour throws its work away and waiting does not.
           alt: { action: 'wait', label: 'or wait for the card',
                  detail: 'leave wan.git alone and start this one when the card has room' } },
  },
}
const failTree = guard('render a health report whose log named a cause',
  () => __fns.HealthReport({ health: failHealth, app: { id: 'comfy.git' }, ctx, onChange: () => {} }))
const failTexts = texts(failTree)
ok('  the named cause is the heading', hasText(failTexts, 'The graphics card ran out of memory'),
   failTexts.join(' | '))
ok('  the reading carries the program\'s own arithmetic, not an estimate',
   hasText(failTexts, '111.76 GiB'), failTexts.join(' | '))
ok('  the sentence that failed is quoted verbatim, for pasting into a search',
   hasText(failTexts, 'OutOfMemoryError'), failTexts.join(' | '))
ok('  a second reading in the same log is named rather than dropped',
   hasText(failTexts, 'out_of_ram'), failTexts.join(' | '))
const failPress = findPress(failTree, 'Stop wan.git')
ok('  the answer to it is one button', typeof failPress === 'function')
ok('  and a report with no failure gets no failure block',
   !findPress(__fns.HealthReport({ health: stepHealth, app: { id: 'comfy.git' }, ctx,
                                   onChange: () => {} }), 'Stop '))
if (typeof failPress === 'function') {
  guard('press the failure\'s fix', () => failPress())
  await new Promise(r => setTimeout(r, 60))
  ok('  it stopped the app holding the card, rather than offering it weights',
     failCalls.some(c => c === 'POST /apps/wan.git/stop'), JSON.stringify(failCalls))
  ok('  and never touched the app it is diagnosing',
     failCalls.every(c => c.indexOf('/apps/comfy.git') === -1
                          && c.indexOf('/models') === -1), JSON.stringify(failCalls))
}
// Two answers, when the cause has two that are both right: closing the neighbour
// frees the card now and throws that work away, waiting starts this app when the
// neighbour finishes on its own. Pressing the second one must queue a launch and
// must not close anything.
const waitPress = findPress(failTree, 'or wait for the card')
ok('  a cause with two right answers offers both', typeof waitPress === 'function')
if (typeof waitPress === 'function') {
  failCalls.length = 0
  guard('press the waiting answer', () => waitPress())
  await new Promise(r => setTimeout(r, 60))
  ok('  it queues the launch for when the card frees',
     failCalls.some(c => c === 'POST /apps/comfy.git/wait'), JSON.stringify(failCalls))
  ok('  and closes nothing to get there',
     failCalls.every(c => c.indexOf('/stop') === -1), JSON.stringify(failCalls))
}

ctx.rest = healthRest

// ---- the shelf explains the disk from the machine's end ---------------------
// The pane that answers "what is this drive full of, and what does it still
// lack". Its loaded state cannot be reached in this harness — setters here are
// no-ops — so what is checked is that the pane mounts, that its controls are
// there before any data arrives, and that asking for the shelf is one call to
// the route that answers it.
const shelfRoutes = []
const prevShelfRest = ctx.rest
ctx.rest = async (p) => {
  shelfRoutes.push(p)
  if (p.indexOf('/models') === 0) {
    return { apps: [{ app: 'comfy', ready: false, count: 0 }], need_weights: ['comfy'],
             libraries: [{ folder: 'E:/AI MODELS/models', apps: [], empty: true,
                           total: 0, bytes: 1024 }],
             cached: { repos: [{ repo: 'owner/thing', bytes: 4096 }], count: 1, bytes: 4096 },
             summary: '0/1 apps can generate; 1 weight library holding 1 KB' }
  }
  return {}
}
const shelfTree = guard('render the weight shelf with no data yet',
                        () => __fns.WeightShelf({ ctx }))
if (shelfTree) {
  // Props before descent: a node whose type is a component would otherwise be
  // replaced by its render before anything could look at what it was given.
  const findEl = (node, pred) => {
    if (Array.isArray(node)) {
      for (const k of node) { const h = findEl(k, pred); if (h) return h }
      return null
    }
    if (!node || typeof node !== 'object') return null
    if (pred(node)) return node
    if (typeof node.type === 'function') return findEl(node.type(node.props || {}), pred)
    return findEl(node.props && node.props.children, pred)
  }
  const findOne = (pred) => findEl(shelfTree, pred)
  const head = findOne(n => n.props && n.props.children === 'Weights on this disk')
  ok('  the shelf says what it is before it says anything else', !!head)
  const box = findOne(n => n.props && n.props['aria-label'] === 'Repository to pull')
  ok('  a repository is named, not a path', !!box)
  const target = findOne(n =>
    n.props && n.props['aria-label'] === 'App the pulled file should serve')
  ok('  and the app it should serve is chosen, so the file lands somewhere readable', !!target)
  const pullBtn = findOne(n => n.props && n.props.children === 'Pull')
  ok('  the pull is a button', !!pullBtn)
  ok('  and it refuses to start with nothing named',
     !!pullBtn && pullBtn.props.disabled === true)
  __effects.splice(0).forEach(fn => { try { fn() } catch (e) { /* async load */ } })
  await new Promise(r => setTimeout(r, 80))
  ok('  opening it asks for the shelf, and it is one route',
     shelfRoutes.filter(p => p === '/models').length === 1, JSON.stringify(shelfRoutes))
}
ctx.rest = prevShelfRest

// ---- what needs you: the report, at the top of the System window ------------
// One route, read at most once, and every row says what to do in words. The point
// of the panel is that "is anything wrong here" is answered by a sentence rather
// than by a list someone has to interpret — and that the buttons which *act* stay
// on the app's card instead of appearing twice with two different behaviours.
const reportRoutes = []
const prevReportRest = ctx.rest
// As the route answers it. Held as a value because the panel that draws it is a
// plain function of its data (`needsPanel`): the mount is checked for reading it,
// and the drawing is checked without a state update the harness cannot do.
const REPORT_STUB = {
  ok: true,
  headline: '1 app that cannot run · 1 update waiting · 1 app that cannot generate yet',
  needs: [
    { kind: 'broken', app: 'wan.git', title: 'Code', detail: 'no launcher',
      needs_install: true, fixable: true,
      next_step: { action: 'install', label: 'Install it again' } },
    { kind: 'update', app: 'comfy.git', behind: 3, title: 'an update is waiting',
      next_step: { action: 'update', label: 'Update it' } },
    { kind: 'weights', apps: ['box.git'], title: '1 app cannot generate right now',
      detail: 'no weight library is mapped, and the largest is E:/AI MODELS/models',
      next_step: { action: 'point_models', folder: 'E:/AI MODELS/models',
                   label: 'Point them at the weights on this disk' } },
  ],
  waiting: [{ app: 'wan.git', action: 'launch', jid: 'wait-1' }],
  counts: { apps: 7, broken: 1 },
  notes: ['health and weights were read from disk for 7 app folders',
          'the update answers here are the cached ones: this report never reaches '
          + 'the network'],
}
ctx.rest = async (p) => {
  reportRoutes.push(p)
  if (p === '/report') return REPORT_STUB
  return {}
}
const reportTree = guard('render what needs you', () => __fns.NeedsYou({ ctx }))
if (reportTree) {
  const findEl2 = (node, pred) => {
    if (Array.isArray(node)) {
      for (const k of node) { const h = findEl2(k, pred); if (h) return h }
      return null
    }
    if (!node || typeof node !== 'object') return null
    if (pred(node)) return node
    if (typeof node.type === 'function') return findEl2(node.type(node.props || {}), pred)
    return findEl2(node.props && node.props.children, pred)
  }
  const texts2 = (node, out = []) => {
    if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
    if (Array.isArray(node)) { node.forEach((k) => texts2(k, out)); return out }
    if (node && typeof node === 'object') texts2(node.props && node.props.children, out)
    return out
  }
  const drawn = guard('draw the report it read',
                      () => __fns.needsPanel(REPORT_STUB, {}))
  const panel = drawn || reportTree
  const head2 = findEl2(reportTree, n => n.props
    && n.props.children === 'What needs you')
  ok('  the panel says what it is before it says anything else', !!head2)
  ok('  and it says it in the loaded view too, so the heading is not a loading artefact',
     !!findEl2(panel, n => n.props && n.props.children === 'What needs you'))
  ok('  before any data arrives it says it is reading, not that nothing is wrong',
     texts2(reportTree).some(t => t.indexOf('Reading what this machine already knows') === 0),
     JSON.stringify(texts2(reportTree)))
  __effects.splice(0).forEach(fn => { try { fn() } catch (e) { /* async load */ } })
  await new Promise(r => setTimeout(r, 80))
  ok('  opening it asks the report route, once',
     reportRoutes.filter(p => p === '/report').length === 1, JSON.stringify(reportRoutes))
  const words = texts2(panel)
  ok('  the headline is shown as a sentence',
     words.some(t => t.indexOf('1 app that cannot run') === 0), JSON.stringify(words))
  ok('  a row says what to do in words, so it is read rather than interpreted',
     words.includes('\u2192 Install it again') && words.includes('\u2192 Update it'),
     JSON.stringify(words))
  ok('  and the weights row names the library already on this disk',
     words.some(t => t.indexOf('\u2192 Point them at the weights') === 0), JSON.stringify(words))
  ok('  a queued launch is explained, including that closing the panel forgets it',
     words.some(t => t.indexOf('A launch is queued for the card') === 0)
     && words.some(t => t.indexOf('closing it forgets the wait') > 0), JSON.stringify(words))
  ok('  what was read is shown, and that nothing was fetched',
     words.some(t => t.indexOf('never reaches the network') > 0), JSON.stringify(words))
  const showBtn = findEl2(panel, n => n.props && n.props.children === 'Show it')
  ok('  the only action a row offers is a route to the app itself', !!showBtn)
  if (showBtn) {
    const prevTab = tabAtom.get()
    showBtn.props.onClick()
    ok('  and pressing it lands on the apps list',
       tabAtom.get() === 'apps', String(tabAtom.get()))
    tabAtom.set(prevTab)
  }
  ok('  it does not offer the button that acts: that lives on the app card',
     !words.includes('Install it') && !words.includes('Update it'),
     JSON.stringify(words.filter(t => t.indexOf('it') > 0)))
}
ctx.rest = prevReportRest

// ---- what an app will ask you for ------------------------------------------
// The badge is a pure function of one cached answer, so the claims about it are
// checkable without mounting anything: that "we have not looked" is not the same
// as "nothing needed", that a key Hermes already answers is never called
// something you must go and find, and that the filter and the badge agree — a
// card badged "needs 1 key" that the filter leaves out is a filter nobody can
// act on. The card is then checked for *reading* that answer off its own payload
// rather than fetching it, because a render is the last place a folder walk
// belongs.
const KB = (over) => Object.assign(
  { scanned: true, local: false, complete: true, keys: [], keys_total: 0,
    held_count: 0, cloud_total: 0 }, over)
const kbCases = [
  ['never read', undefined, 'scripts not read', ''],
  ['never read, empty answer', {}, 'scripts not read', ''],
  ['read with nothing wanted', KB({ local: true }), 'runs here', 'run'],
  ['read but a file was too big', KB({ local: false, complete: false }), 'partly read', ''],
  ['one key nobody holds',
   KB({ keys: [{ name: 'OPENAI_API_KEY', provider: 'openai', held: false }],
        keys_total: 1 }), 'needs 1 key', 'need'],
  ['two keys, one answered',
   KB({ keys: [{ name: 'OPENAI_API_KEY', held: true },
               { name: 'ANTHROPIC_API_KEY', held: false }],
        keys_total: 2, held_count: 1 }), 'needs 1 key', 'need'],
  ['every key answered',
   KB({ keys: [{ name: 'OPENAI_API_KEY', held: true }], keys_total: 1, held_count: 1 }),
   'keys ready', 'run'],
  ['calls out with no key named', KB({ cloud_total: 2 }), 'calls out', 'need'],
]
for (const [label, keys, text, cls] of kbCases) {
  const b = __fns.keyBadge(keys)
  ok(`  ${label}: the badge reads "${text}"`, b.text === text, `${b.text} / ${b.cls}`)
  ok(`  ${label}: tinted ${cls ? cls : 'as a plain badge'}`, b.cls === cls, b.cls)
  ok(`  ${label}: and explains itself, so it is never just a word`,
     typeof b.title === 'string' && b.title.length > 20, String(b.title).slice(0, 40))
}
ok('  a key Hermes already answers is never called something you need',
   __fns.keyBadge(KB({ keys: [{ name: 'HF_TOKEN', held: true }], keys_total: 1,
     held_count: 1 })).text !== 'needs 1 key')
ok('  the keys it wants are named in the explanation',
   __fns.keyBadge(KB({ keys: [{ name: 'REPLICATE_API_TOKEN', held: false }],
     keys_total: 1 })).title.indexOf('REPLICATE_API_TOKEN') > 0)
const filtCases = [
  ['an app nobody read', undefined, false, false],
  ['an app that runs here', KB({ local: true }), true, false],
  ['an app wanting a key nobody holds',
   KB({ keys: [{ name: 'OPENAI_API_KEY', held: false }], keys_total: 1 }), false, true],
  ['an app whose keys are all answered',
   KB({ keys: [{ name: 'HF_TOKEN', held: true }], keys_total: 1, held_count: 1 }), false, false],
  ['an app that calls out', KB({ cloud_total: 1 }), false, true],
  ['a partly read folder where nothing was found',
   KB({ local: false, complete: false }), false, false],
]
for (const [label, keys, isLocal, isNeed] of filtCases) {
  ok(`  "Runs here" ${isLocal ? 'keeps' : 'leaves out'} ${label}`,
     __fns.keysMatch(keys, 'local') === isLocal)
  ok(`  "Needs a key" ${isNeed ? 'keeps' : 'leaves out'} ${label}`,
     __fns.keysMatch(keys, 'need') === isNeed)
  ok(`  and with no filter set, ${label} is shown`,
     __fns.keysMatch(keys, '') === true)
}
const keyRoutes = []
const prevKeyRest = ctx.rest
ctx.rest = async (p) => { keyRoutes.push(p); return {} }
const keyCardTree = guard('render a card for an app whose scripts were read',
  () => __fns.AppCard({ ctx, app: { id: 'wan.git', name: 'wan', description: 'wan',
    keys: KB({ keys: [{ name: 'OPENAI_API_KEY', provider: 'openai', held: false }],
               keys_total: 1 }) }, onChange: async () => {}, menuOpen: true }))
const keyEl = (node, pred) => {
  if (Array.isArray(node)) {
    for (const k of node) { const h = keyEl(k, pred); if (h) return h }
    return null
  }
  if (!node || typeof node !== 'object') return null
  if (pred(node)) return node
  if (typeof node.type === 'function') return keyEl(node.type(node.props || {}), pred)
  return keyEl(node.props && node.props.children, pred)
}
const keyTexts = (node, out = []) => {
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (Array.isArray(node)) { node.forEach((k) => keyTexts(k, out)); return out }
  if (node && typeof node === 'object') keyTexts(node.props && node.props.children, out)
  return out
}
ok('  drawing a card reads no route at all', keyRoutes.length === 0, JSON.stringify(keyRoutes))
ok('  the badge is on the card', keyTexts(keyCardTree).includes('needs 1 key'),
   JSON.stringify(keyTexts(keyCardTree)))
ok('  and reading its scripts is a button, not something a render starts',
   !!keyEl(keyCardTree, n => n.props && n.props.children === 'Read its scripts')
   || !!keyEl(keyCardTree, n => n.props && n.props.children === 'What it needs'))

// ---- a click that failed has to say why ----------------------------------
// The engine routes answer 200 with {ok: false, error} when a start cannot find
// a usable Node, when the spawn dies, when the port never answers. The header
// used to read only the status code, so every one of those looked like a click
// that worked: "Starting…", then "stopped", and no reason anywhere on screen.
ok('  a backend that answers ok:false becomes a sentence rather than a silent success',
   __fns.actionFailure('Start', { ok: false, error: 'No compatible Node runtime found. Athena needs Node 22.x' })
     === 'Start: No compatible Node runtime found. Athena needs Node 22.x',
   __fns.actionFailure('Start', { ok: false, error: 'No compatible Node runtime found. Athena needs Node 22.x' }))
ok('  ... and a real success says nothing at all',
   __fns.actionFailure('Start', { ok: true, ready: true, port: 42001 }) === '')
ok('  ... an ok:false with no explanation still names the action that failed',
   __fns.actionFailure('Stop', { ok: false }) === 'Stop: failed')
ok('  ... and an answer with no body is not read as a failure',
   __fns.actionFailure('Start', undefined) === ''
   && __fns.actionFailure('Start', '') === ''
   && __fns.actionFailure('Start', 'not json') === '')

// ---- the card's shape, after it stopped being a wall of buttons ----------
// The card used to carry ten buttons on every app whatever its state, with the
// description and the tags the quietest things on it. These checks pin the new
// contract: one chip that says the state, one button for what you came to do, one
// for what you reach for next, and everything else behind the menu.
const stateApp = (over) => Object.assign({ id: 'x.git', name: 'x', description: 'd', tags: [] }, over)
const chipOf = (w) => w && w.text
ok('  a running app says so once, with the port Open will use',
   chipOf(__fns.appStateWords(stateApp({ running: true, url: 'http://127.0.0.1:7860' }))) === 'running \u00b7 :7860',
   chipOf(__fns.appStateWords(stateApp({ running: true, url: 'http://127.0.0.1:7860' }))))
ok('  ... and a running app with no address still says running rather than inventing a port',
   chipOf(__fns.appStateWords(stateApp({ running: true }))) === 'running')
ok('  an app with no launcher script says that, because it is the reason it will not start',
   chipOf(__fns.appStateWords(stateApp({ manifest: false }))) === 'no launcher')
ok('  an app nobody has read says so, rather than borrowing the word for a clean app',
   chipOf(__fns.appStateWords(stateApp({}))) === 'nothing read yet',
   chipOf(__fns.appStateWords(stateApp({}))))
ok('  the chip and the key filter say the same thing about the same app',
   chipOf(__fns.appStateWords(stateApp({ keys: KB({ keys: [{ name: 'K', provider: 'p', held: false }],
     keys_total: 1 }) }))) === 'needs 1 key')

const actsIdle = __fns.appActions(stateApp({}))
ok('  what you came to do is the button: Launch', actsIdle.primary.label === 'Launch', actsIdle.primary.label)
ok('  the one you reach for next is Terminal',
   actsIdle.second.label === 'Terminal', actsIdle.second.label)
ok('  and the menu still offers everything the card used to shout, including Publish',
   ['Publish', 'Update', 'Install', 'Test run', 'Check for updates', 'Fix', 'Delete\u2026', 'Read its scripts']
     .every((w) => actsIdle.more.some((a) => a.label === w)),
   actsIdle.more.map((a) => a.label).join(' | '))
ok('  Delete is in the menu as the dangerous one',
   actsIdle.more.filter((a) => a.danger).map((a) => a.label).join(',') === 'Delete\u2026')
ok('  a deleting word is not offered as the one button on a card',
   actsIdle.primary.label !== 'Delete\u2026' && actsIdle.second.label !== 'Delete\u2026')

const actsUp = __fns.appActions(stateApp({ running: true, url: 'http://127.0.0.1:7860' }))
ok('  a running app offers Open, not Launch', actsUp.primary.label === 'Open', actsUp.primary.label)
ok('  ... with Stop beside it, because that is the moment someone wants it',
   actsUp.second.label === 'Stop', actsUp.second.label)
ok('  ... and Terminal moves into the menu rather than being one of two buttons',
   actsUp.more.some((a) => a.label === 'Terminal') && actsUp.second.label !== 'Terminal')
ok('  an app with no launcher offers Install as its button, since nothing else can happen',
   __fns.appActions(stateApp({ manifest: false })).primary.label === 'Install')
ok('  and Install is not offered twice when it is already the button',
   __fns.appActions(stateApp({ manifest: false })).more.filter((a) => a.label === 'Install').length === 0)

const closedTree = __fns.AppCard({ ctx, app: stateApp({}), onChange: async () => {} })
const buttonsIn = (node, out = []) => {
  if (Array.isArray(node)) { node.forEach((k) => buttonsIn(k, out)); return out }
  if (!node || typeof node !== 'object') return out
  const t = node.type
  if (t === 'button' || t === 'Button' || (node.props && typeof node.props.onClick === 'function')) out.push(node)
  if (typeof t === 'function') buttonsIn(t(node.props || {}), out)
  else buttonsIn(node.props && node.props.children, out)
  return out
}
const face = buttonsIn(closedTree)
ok('  the card face is three controls, not ten',
   face.length <= 3, `found ${face.length}: ` + keyTexts(closedTree).filter((s) => /^[A-Z]/.test(s)).join(' | '))
ok('  and the menu is not in the tree until it is opened, so nothing can pass by finding a hidden button',
   !keyTexts(closedTree).includes('Publish'), keyTexts(closedTree).join(' | '))
ok('  the old line of text about unread scripts is gone from the card face',
   !keyTexts(closedTree).includes('scripts not read'))

const rowTree = __fns.AppCard({ ctx, app: stateApp({}), onChange: async () => {}, row: true })
const clsOf = (node) => {
  if (Array.isArray(node)) { for (const k of node) { const c = clsOf(k); if (c) return c } return null }
  if (!node || typeof node !== 'object') return null
  if (node.props && /ah-lrow/.test(String(node.props.className))) return node.props.className
  if (typeof node.type === 'function') return clsOf(node.type(node.props || {}))
  return clsOf(node.props && node.props.children)
}
ok('  the row layout is the same card with one class added, not a second renderer',
   clsOf(rowTree) === 'ah-card ah-lrow', String(clsOf(rowTree)))
const hasCls = (node, re) => {
  if (Array.isArray(node)) return node.some((k) => hasCls(k, re))
  if (!node || typeof node !== 'object') return false
  if (node.props && re.test(String(node.props.className))) return true
  if (typeof node.type === 'function') return hasCls(node.type(node.props || {}), re)
  return hasCls(node.props && node.props.children, re)
}
ok('  ... and it clips the description instead of dropping it',
   hasCls(rowTree, /ah-clip/) && !hasCls(closedTree, /ah-clip/))

ctx.rest = prevKeyRest

// ---- the notice a finished app job sends -------------------------------
// A run has an artifact and the artifact is the interesting half — so the check
// here is mostly about the two ways to lie: naming a file that was not produced,
// and reporting a duration the record does not have.
const RW = (over) => Object.assign(
  { app: 'RMBG-2-Studio', state: 'ok', seconds: 600,
    files: [{ name: 'out.png', path: '/runs/a/out.png', kind: 'image' }] }, over)
const w1 = __fns.runDoneWords(RW({}))
ok('  a finished run names the file it made, not just that it finished',
   /finished in 10m 0s — out\.png$/.test(w1.text), w1.text)
ok('  a finished run is not an error', w1.kind === 'info' && w1.quiet === false)
const w2 = __fns.runDoneWords(RW({ seconds: 45,
  files: [{ name: 'out.png', path: '/a/out.png' }, { name: 'mask.png', path: '/a/mask.png' }] }))
ok('  a second file is counted rather than hidden', /out\.png \(\+1 more\)$/.test(w2.text), w2.text)
ok('  under a minute reads in seconds', /in 45s /.test(w2.text), w2.text)
const w3 = __fns.runDoneWords(RW({ seconds: 7322 }))
ok('  hours are hours, not 122 minutes', /in 2h 2m /.test(w3.text), w3.text)
const w4 = __fns.runDoneWords(RW({ seconds: 3 }))
ok('  a run the agent watched finish is marked quiet rather than announced',
   w4.quiet === true && w4.text.length > 0, `${w4.quiet} / ${w4.text}`)
const w5 = __fns.runDoneWords(RW({ files: [], seconds: 300 }))
ok('  a run that answered but produced nothing says exactly that',
   /but no file came out/.test(w5.text) && !/out\.png/.test(w5.text), w5.text)
const w6 = __fns.runDoneWords(RW({ state: 'failed', files: [], seconds: 30,
  error: 'RuntimeError: no checkpoint\nsecond line' }))
ok('  a failed run is an error and carries the app own reason, first line only',
   w6.kind === 'error' && /no checkpoint/.test(w6.text) && !/second line/.test(w6.text), w6.text)
const w7 = __fns.runDoneWords(RW({ seconds: null }))
ok('  an unknown duration is left out rather than rounded to zero',
   !/ in /.test(w7.text) && !/0s/.test(w7.text), w7.text)
ok('  and an unknown duration never marks a run quiet',
   w7.quiet === false, String(w7.quiet))
const w8 = __fns.runDoneWords({})
ok('  a run record with nothing in it still yields a sentence',
   typeof w8.text === 'string' && w8.text.length > 10, w8.text)
ok('  the likely output is preferred over the first file',
   /wanted\.png/.test(__fns.runDoneWords(RW({ likely_output: '/runs/a/wanted.png',
     files: [{ name: 'other.png', path: '/runs/a/other.png' },
             { name: 'wanted.png', path: '/runs/a/wanted.png' }] })).text))

// ---- where an app has been, and going back ---------------------------------
// Read from the app its own repository (a reflog entry exists for every commit
// this folder has stood on), so the list is never a guess at a "previous version".
// The panel is a plain function of its data, so every state is drawn here: a
// folder that is not a repository, an app that has only ever been on one commit,
// a row that is where the app is now, a working tree that would lose changes, and
// the callback a Put back button actually fires.
const mkEl = (node, pred) => {
  if (Array.isArray(node)) { for (const k of node) { const h = mkEl(k, pred); if (h) return h } return null }
  if (!node || typeof node !== 'object') return null
  if (pred(node)) return node
  if (typeof node.type === 'function') return mkEl(node.type(node.props || {}), pred)
  return mkEl(node.props && node.props.children, pred)
}
const mkTexts = (node, out = []) => {
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (Array.isArray(node)) { node.forEach((k) => mkTexts(k, out)); return out }
  if (node && typeof node === 'object') mkTexts(node.props && node.props.children, out)
  return out
}
const HIST = {
  ok: true, is_repo: true, branch: 'main', dirty: 0, ahead: 0, can_revert: true,
  head: { sha: 'aaaa1111222233334444', short: 'aaaa11112222', subject: 'change 1' },
  commits: [
    { sha: 'aaaa1111222233334444', short: 'aaaa11112222', when: '2026-10-14 09:20:00 -0400',
      what: 'pull: Fast-forward', current: true },
    { sha: 'bbbb5555666677778888', short: 'bbbb55556666', when: '2026-10-11 22:20:49 -0400',
      what: 'clone: from https://github.com/owner/app.git', current: false },
  ],
}
const pbTexts = (data, opts) => mkTexts(guard('drawing the history',
  () => __fns.putBackPanel(data, opts)) || [])

const pbBroken = pbTexts({ ok: false, error: 'the repository did not answer in time' })
ok('  a history that could not be read says so rather than showing an empty list',
   pbBroken.join(' ').includes('did not answer'), pbBroken.join(' | '))
const pbPlain = pbTexts({ ok: true, is_repo: false,
  reason: 'this app was not installed from a git repository, so it has no commits to go back to' })
ok('  an app that is not a repository is told, not offered a button',
   pbPlain.join(' ').includes('not installed from a git repository'), pbPlain.join(' | '))
const pbOne = pbTexts({ ok: true, is_repo: true, can_revert: false, dirty: 0, ahead: 0,
  commits: [{ sha: 'cccc', short: 'cccc00001111', when: '2026-10-01 08:00:00 -0400',
              what: 'clone: from origin', current: true }],
  reason: 'this app has only ever been on one commit, so there is nothing to put back to' })
ok('  an app with one commit in its whole history is not offered a pointless button',
   pbOne.join(' ').includes('only ever been on one commit'), pbOne.join(' | '))

const pbTree = guard('drawing where the app has been', () => __fns.putBackPanel(HIST, { onPutBack: () => {} }))
const pb = mkTexts(pbTree || [])
ok('  every commit this app has stood on is listed, newest first',
   pb.join(' ').includes('aaaa11112222') && pb.join(' ').includes('bbbb55556666')
   && pb.indexOf('aaaa11112222') < pb.indexOf('bbbb55556666'), pb.join(' | '))
ok('  each row carries when it was there and what moved it',
   pb.join(' ').includes('2026-10-14 09:20:00') && pb.join(' ').includes('pull: Fast-forward')
   && pb.join(' ').includes('clone: from https://github.com/owner/app.git'), pb.join(' | '))
ok('  the row the app is on now is labelled rather than offered',
   pb.join(' ').includes('where it is now') && pb.includes('Put back'), pb.join(' | '))
ok('  the wording is about commits it has been on, never a guessed-at previous version',
   !/previous version|roll ?back|last known good/i.test(pb.join(' ')), pb.join(' | '))
ok('  and it says plainly that going back leaves the app behind its remote',
   pb.join(' ').includes('behind its remote again'), pb.join(' | '))
ok('  a clean tree is not warned about', !pb.join(' ').includes('would give up'), pb.join(' | '))
const pbDirty = mkTexts(guard('drawing a history with local work in it',
  () => __fns.putBackPanel({ ...HIST, dirty: 3, ahead: 1 }, {})) || [])
ok('  changes that would be lost are counted before anyone agrees to anything',
   pbDirty.join(' ').includes('3 files with changes that are not in a commit')
   && pbDirty.join(' ').includes('1 commit made in this folder'), pbDirty.join(' | '))
ok('  and the one that would be given up is named as a singular',
   mkTexts(__fns.putBackPanel({ ...HIST, dirty: 1 }, {})).join(' ').includes('1 file with changes'),
   mkTexts(__fns.putBackPanel({ ...HIST, dirty: 1 }, {})).join(' | '))

// The button has to hand back the commit it is drawn beside — the driver for a
// revert is the backend, and it re-validates the sha against the same reflog, so a
// wrong sha here is a refusal rather than a silent reset to somewhere else.
let putBackAsked = null
const pbBtn = mkEl(guard('the put back button', () => __fns.putBackPanel(HIST,
  { onPutBack: (c) => { putBackAsked = c } })), (n) => n.props && n.props.children === 'Put back')
ok('  the row that is not where the app is now is the one offering to go back',
   !!pbBtn, String(pbBtn))
if (pbBtn) {
  pbBtn.props.onClick()
  ok('  and the button hands back the commit it was drawn beside, not the newest',
     putBackAsked && putBackAsked.short === 'bbbb55556666', JSON.stringify(putBackAsked))
}
const pbBusy = mkTexts(__fns.putBackPanel(HIST, { busy: 'bbbb5555666677778888' }))
ok('  while a put-back is running the button says so instead of inviting a second one',
   pbBusy.join(' ').includes('Putting back'), pbBusy.join(' | '))
ok('  a history with nothing in it still yields a sentence rather than a blank',
   mkTexts(__fns.putBackPanel({ ok: true, is_repo: true, commits: [] })).join(' ').length > 10,
   mkTexts(__fns.putBackPanel({ ok: true, is_repo: true, commits: [] })).join(' | '))

// ---- what a running job says about itself -------------------------------
// The panel used to print "working…" for every running job, which is also true
// of a hung one. These are the two ages that replace it, and the rule that keeps
// the header from ever promising a duration Athena has no record to base on.
const JP = (over) => Object.assign(
  { kind: 'install', state: 'running', elapsed: 600, since_output: 2, since_move: 2 }, over)
const j1 = __fns.jobPace(JP({}))
ok('  a job that just printed says how long ago', /working — output 2s ago$/.test(j1.text), j1.text)
ok('  and that is the good state, not a warning', j1.tone === 'ok', String(j1.tone))
const j2 = __fns.jobPace(JP({ since_output: 40, since_move: 40 }))
ok('  a silence under a minute and a half is stated plainly, without alarm',
   /^quiet for 40s$/.test(j2.text) && j2.tone === '', `${j2.text} / ${j2.tone}`)
const j3 = __fns.jobPace(JP({ since_output: 372, since_move: 372 }))
ok('  a long silence is a warning that names what an install is doing while quiet',
   j3.tone === 'warn' && /no output for 6m 12s/.test(j3.text)
     && /walks the whole repository/.test(j3.text), j3.text)
const j4 = __fns.jobPace(JP({ kind: 'nonsense-kind', since_output: 300, since_move: 300 }))
ok('  an unknown kind still says the silence, just without a guess at the cause',
   j4.tone === 'warn' && /no output for 5m 0s$/.test(j4.text), j4.text)
ok('  and the kind is the job record own word, lowercased',
   /compile for a while/.test(__fns.jobPace(JP({ kind: 'Build', since_output: 300, since_move: 300 })).text))
// A bar that moved a second ago is a sign of life even if nothing was printed —
// a copy reports progress and no output at all, and reading that as "silent for
// six minutes" would be a lie about a job that is plainly working.
const j5 = __fns.jobPace(JP({ kind: 'copy', elapsed: 900, since_output: 600, since_move: 1 }))
ok('  a job that only moved its bar is working, not silent',
   /working — progress 1s ago$/.test(j5.text) && j5.tone === 'ok', j5.text)
const j6 = __fns.jobPace(JP({ elapsed: 1, since_output: 1, since_move: 1 }))
ok('  a job one second old is starting, not quiet', /just started$/.test(j6.text), j6.text)
const j7 = __fns.jobPace(JP({ state: 'done', elapsed: 252 }))
ok('  a finished job reports what it took', /took 4m 12s$/.test(j7.text) && j7.tone === 'ok', j7.text)
const j8 = __fns.jobPace(JP({ state: 'failed', elapsed: 63 }))
ok('  a failed job says it gave up, and after how long',
   /gave up after 1m 3s$/.test(j8.text) && j8.tone === 'warn', j8.text)
const j9 = __fns.jobPace(JP({ since_output: null, since_move: null }))
ok('  a job with no usable age falls back to the plain word rather than inventing one',
   j9.text === 'running' && j9.tone === '', `${j9.text} / ${j9.tone}`)
ok('  and it still says why it cannot be more specific',
   /no usable|no record|will not pretend/.test(j9.title), j9.title)
ok('  nothing at all yields nothing rather than a bad sentence',
   __fns.jobPace(null).text === '', String(__fns.jobPace(null).text))
ok('  no wording anywhere promises an expected duration',
   !/expect|usually takes \d|should take/i.test(
     [j1, j3, j5, j7, j8, j9].map((w) => w.text + ' ' + w.title).join(' ')),
   j3.title)
ok('  the title carries both ages, so hovering answers the question too',
   /running 10m 0s/.test(j3.title) && /last output 6m 12s ago/.test(j3.title), j3.title)
ok('  and admits it does not know how long this one usually takes',
   /no record of how long/.test(j3.title), j3.title)

// ---- a job the panel was watching when the engine restarted ------------------
// A job is an id in the engine's process, so a restart takes it. The next poll of
// a one-and-a-half-second loop then answers 404, which the panel used to report as
// the request failing — a network fault that did not happen, for a person watching
// an install. The backend names the restart in the body; this is what the panel
// says when it reads it, and when all it has is an error text.
const gone = __fns.jobGoneWords
ok('  a job that vanished with a restart says so, rather than blaming the request',
   /not in this run of Hermes/.test(gone({ message: 'no such job' }, 'request failed')),
   gone({ message: 'no such job' }, 'request failed'))
ok('  the engine\'s own sentence wins when the body carries one',
   gone({ body: { gone_with_restart: true, note: 'BACKEND WORDS' } }, 'x') === 'BACKEND WORDS')
ok('  and a failure that is not a restart is passed through untouched',
   gone({ message: 'Forbidden' }, 'Forbidden') === 'Forbidden')
ok('  the sentence points at the app\'s own log rather than promising a retry',
   /log/.test(gone({ message: 'no such job' }, ''))
     && !/try again later|temporar/i.test(gone({ message: 'no such job' }, '')))

// ---- no card advertises a platform ------------------------------------------
// A store row used to carry a badge saying "runs on Windows", assembled from
// other people's check-ins — so an app made for a Mac could wear it just because
// a Windows user had once run it. On a card that reads as a requirement about the
// app, which it never was. The badge is gone; the chip's filter stays, because
// the chip is opt-in and its title says exactly what it counts.
const RB = (over) => Object.assign({ checkins: 12, platforms: {}, gpus: {} }, over)
const rigCases = [
  ['checked in from Windows + NVIDIA',
   RB({ platforms: { windows: true, linux: true }, gpus: { nvidia: true } }), true],
  ['checked in from Windows only',
   RB({ platforms: { windows: true }, gpus: { amd: true } }), false],
  ['checked in from mac only',
   RB({ platforms: { mac: true }, gpus: { apple: true } }), false],
  ['no check-ins at all', RB({ checkins: 0 }), false],
]
const PLATFORM_WORDS = /runs on Windows|Windows, no NVIDIA|no Windows check-in|nobody has run it/
for (const [label, app, keeps] of rigCases) {
  const cardText = texts(__fns.StoreCard({
    ctx: instCtx, app: Object.assign({ slug: 'x', name: 'X' }, app), onInstalled: () => {},
  })).join(' ')
  ok(`  ${label}: the card carries no platform badge`, !PLATFORM_WORDS.test(cardText),
     cardText.slice(0, 120))
  ok(`  the chip ${keeps ? 'keeps' : 'leaves out'} the card: ${label}`,
     __fns.rigMatch(app, true) === keeps)
  ok(`  with the chip off, ${label} is shown`, __fns.rigMatch(app, false) === true)
}


// ---- the store no longer sells agent control --------------------------------
// The shelf used to carry a badge, a line of copy and a filter saying an
// installed app could be driven by an agent. That promise is not yet true of
// every app, so it is out of the store and off the card — and these checks are
// falsifiable both ways: the copy they read before fails them.
const shelfTexts = (node, out = []) => {
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (Array.isArray(node)) { node.forEach((k) => shelfTexts(k, out)); return out }
  if (node && typeof node === 'object') shelfTexts(node.props && node.props.children, out)
  return out
}
const AGENT_CLAIM = /agent[- ]ready|agent[- ]controlled|exposed to agents|native tool/i
const storeRest = ctx.rest
ctx.rest = async (p) => {
  if (p.indexOf('/catalog/tags') === 0) return { tags: [] }
  if (p.indexOf('/catalog') === 0) return { apps: [], total: 4289 }
  return storeRest(p)
}
const storeTree = guard('render the store tab with an empty page',
                        () => __fns.StoreTab({ ctx, st: {}, refresh: async () => {} }))
if (storeTree) {
  const blob = shelfTexts(storeTree).join(' ')
  ok('  the store promises nothing about what a caller can do to an app',
     !AGENT_CLAIM.test(blob), blob.slice(0, 160))
  ok('  and there is no filter left for a claim the store no longer makes',
     findNode(storeTree, (n) => /agent/i.test(String((n.props && n.props.children) || ''))).length === 0)
  ok('  the rig filter it does keep still narrows the page on screen',
     findNode(storeTree, (n) => n.props && n.props.children === 'Windows + NVIDIA').length === 1)
}
const cardTree = guard('render a converted store card',
  () => __fns.StoreCard({ ctx, app: { slug: 'rmbg', name: 'RMBG', store: 'athena',
                                      converted: true, tags: [], description: '' },
                          onInstalled: () => {} }))
if (cardTree) {
  const cardBlob = shelfTexts(cardTree).join(' ')
  ok('  a converted card carries no such claim either', !AGENT_CLAIM.test(cardBlob))
  const badge = findNode(cardTree,
    (n) => n.props && /agent can call it/.test(n.props.title || ''))
  ok('  and nothing on it explains a badge that is gone', badge.length === 0)
}
ctx.rest = storeRest

// ---- what the folder picker says it is looking at ----------------------------
// The picker is the one place in the panel that names a directory, and it is a
// sentence rather than a path: "C:\Users — 12 folders, 3 files". It is read off
// whatever the backend answered, including the answers that are not a listing,
// because a picker that draws an empty box where a directory was is a picker that
// looks like the folder is empty.
{
  const w = __fns.folderWords
  ok('  before anything is read the picker is waiting, not blank',
     w({}) === 'Reading folders\u2026', String(w({})))
  ok('  a folder it cannot read says so, and says which folder',
     w({ path: 'E:\\locked', ok: false, error: 'Access is denied' })
       === 'E:\\locked \u2014 Access is denied', String(w({ path: 'E:\\locked', ok: false, error: 'Access is denied' })))
  ok('  a refusal with no reason still names the folder rather than nothing',
     w({ path: 'E:\\locked', ok: false }) === 'E:\\locked \u2014 cannot be read',
     String(w({ path: 'E:\\locked', ok: false })))
  ok('  a real listing counts folders and files, and counts one as one',
     w({ path: 'E:\\iso', ok: true, dirs: ['a'], files: 1 }) === 'E:\\iso \u2014 1 folder, 1 file',
     String(w({ path: 'E:\\iso', ok: true, dirs: ['a'], files: 1 })))
  ok('  and says nothing about files when it found none',
     w({ path: 'E:\\iso', ok: true, dirs: ['a', 'b'], files: 0 }) === 'E:\\iso \u2014 2 folders',
     String(w({ path: 'E:\\iso', ok: true, dirs: ['a', 'b'], files: 0 })))
}

// ---- what the card has cost -------------------------------------------------
// A running total folded out of the monitor's own samples, and emphatically not a
// meter. The sentence is the whole feature, so every state the record can be in is
// drawn here: nothing sampled yet, a card that reports no power, energy with no
// price, an idle card, a total too small for two decimals, and the one case where
// a number is real. What must never appear is the claim the record cannot back.
const costText = (d) => __fns.costLine(d)
const costNote = (d) => __fns.costDetail(d)

const cIdle = costText({})
ok('  nothing sampled yet says so, rather than showing a zero',
   /Nothing measured yet/.test(cIdle), cIdle)
const cZero = costText({ samples: 4, seconds: 0 })
ok('  a record that has read the card but counted no time claims no total',
   /no time counted yet/.test(cZero) && !/kWh/.test(cZero) && !/\$/.test(cZero), cZero)

const cNoPower = costText({ samples: 40, seconds: 20 * 3600 + 20 * 60, busy_seconds: 3 * 3600,
                            kwh: null, kwh_busy: null, cost: null, price: 0 })
ok('  a card that does not report power draw gets no energy number at all',
   /does not report power draw/.test(cNoPower) && !/kWh/.test(cNoPower), cNoPower)
ok('  and certainly not a cost, which would be the same absent reading twice',
   !/\$/.test(cNoPower), cNoPower)
ok('  while the time it has already spent is still reported',
   /20h 20m/.test(cNoPower), cNoPower)

const cPlain = costText({ samples: 40000, seconds: 20 * 3600 + 20 * 60,
                          busy_seconds: 20 * 3600 + 20 * 60, kwh: 12.4, cost: 2.6, price: 0.21 })
ok('  the ordinary case: the time, the energy, and the price that priced it',
   /20h 20m of card time/.test(cPlain) && /12\.40 kWh/.test(cPlain), cPlain)
ok('  and the money is attributed to the rate rather than stated bare',
   /about \$2\.60 at \$0\.21\/kWh/.test(cPlain), cPlain)

const cNoPrice = costText({ samples: 400, seconds: 2 * 3600, busy_seconds: 10 * 60,
                            kwh: 1.2, cost: null, price: 0 })
ok('  energy with no price set is never shown in money',
   /no price set/.test(cNoPrice) && !/\$/.test(cNoPrice), cNoPrice)
ok('  and the working half of an idle card is separated rather than implied',
   /2h 0m of card time/.test(cNoPrice) && /10m of it with the card working/.test(cNoPrice),
   cNoPrice)

// 9 seconds at 300 W is 0.00075 kWh. At two decimals that is 0.00 — a card that
// has just done work reading as a card that did nothing.
const cTiny = costText({ samples: 4, seconds: 9, busy_seconds: 9,
                         kwh: 0.00075, cost: null, price: 0 })
ok('  a small total keeps its places instead of rounding to nothing',
   /0\.00075 kWh/.test(cTiny), cTiny)
const cTinyCost = costText({ samples: 4, seconds: 9, busy_seconds: 9,
                             kwh: 0.00075, cost: 0.0002, price: 0.21 })
ok('  and money below a cent is not rounded down to "$0.00"',
   /\$0\.0002/.test(cTinyCost), cTinyCost)

ok('  a stretch of the card\u2019s time is worded the way it is said out loud',
   __fns.costSpan(41 * 3600 + 20 * 60) === '1d 17h'
   && __fns.costSpan(2 * 86400 + 5 * 3600) === '2d 5h'
   && __fns.costSpan(12 * 60) === '12m'
   && __fns.costSpan(0) === 'less than a minute',
   [__fns.costSpan(41 * 3600 + 20 * 60), __fns.costSpan(2 * 86400 + 5 * 3600),
    __fns.costSpan(12 * 60), __fns.costSpan(0)].join(' | '))

const cSmallPrint = costNote({ samples: 40000, seconds: 100, kwh: 1.2, cost: 0.25,
                               price: 0.21, peak_watts: 350, gaps: 3,
                               since: '2026-10-14T03:56:08' })
ok('  the small print says when it was counting and when it was not',
   /counted only while Hermes was open/.test(cSmallPrint)
   && /3 gaps not counted/.test(cSmallPrint), cSmallPrint)
ok('  it says the number is an estimate rather than a meter',
   /estimate rather than a meter/.test(cSmallPrint), cSmallPrint)
ok('  it names the peak it saw and when the record began, in that order',
   cSmallPrint.indexOf('peak 350 W') > 0
   && cSmallPrint.indexOf('since 2026-10-14 03:56:08') > cSmallPrint.indexOf('peak 350 W'),
   cSmallPrint)
const cNoPrint = costNote({})
ok('  and an empty record does not describe a total it does not have',
   /only grows while Hermes is open/.test(cNoPrint) && !/peak/.test(cNoPrint), cNoPrint)

const costTree = guard('render the cost card',
  () => __fns.GpuCostCard({ ctx }))
if (costTree) {
  const costTextsAll = mkTexts(costTree).join(' ')
  ok('  the card names itself in the System window',
     costTextsAll.indexOf('What the card has cost') >= 0, costTextsAll.slice(0, 80))
  ok('  the price is offered with the unit it is in',
     costTextsAll.indexOf('Price per kWh') >= 0)
  const nums = mkEl(costTree, (n) => n.props && n.props.type === 'number')
  ok('  and there is somewhere to type it', !!nums)
  const saves = mkEl(costTree, (n) => n.props && n.props.children === 'Save')
  ok('  a Save for the price, and a way to start the record over', !!saves
     && !!mkEl(costTree, (n) => n.props && n.props.children === 'Start over'))
}

// ------------------------------------------- a file, and who would take it
// The routing answer is a sentence per app, and the interesting cases are the
// ones where something is refused: a router that names the wrong app costs a
// person more than one that says it does not know.

const pIdle = __fns.acceptsPanel(null, {})
ok('  before a file is chosen the panel says how to choose one, rather than '
   + 'showing an empty list',
   pIdle.state === 'idle' && /Right-click a file/.test(pIdle.head) && !pIdle.rows.length,
   pIdle.head)

const pRefused = __fns.acceptsPanel({ error: 'there is no file at C:/x/y.png' }, {})
ok('  an answer that was refused says why, and offers nothing to click',
   pRefused.state === 'refused' && pRefused.head === 'there is no file at C:/x/y.png'
   && !pRefused.rows.length && !pRefused.notes.length, pRefused.head)

const pOne = __fns.acceptsPanel({
  ok: true, name: 'cat.png', kind: 'image', fits: [{
    app: 'RMBG-2-Studio.git', app_name: 'RMBG-2-Studio', capability: 'rmbg',
    intent: 'remove_background', tier: 'exact', wants: 'image', input_name: 'image',
    multi: false, running: true,
  }], refused: [], unprobed: [],
}, {})
ok('  one app that would take it: named, with the file itself in the sentence',
   pOne.state === 'ready' && pOne.head === 'cat.png \u2014 one app here would take it.',
   pOne.head)
ok('  the row says what the app takes and where the file goes, in the app\u2019s own '
   + 'vocabulary',
   /takes an image/.test(pOne.rows[0].line) && /\u201cimage\u201d/.test(pOne.rows[0].line),
   pOne.rows[0].line)
ok('  and whether it is up already, because that is the difference between now '
   + 'and a wait', pOne.rows[0].note === 'running now', pOne.rows[0].note)

const pMany = __fns.acceptsPanel({
  name: 'clip.mp4', kind: 'video', fits: [
    { app: 'wan.git', app_name: 'wan', capability: 't2v', intent: 'text_to_video',
      tier: 'exact', wants: 'video', input_name: 'video', multi: false, running: false },
    { app: 'comfy.git', app_name: 'comfy', capability: 'run', intent: 'run_a_workflow',
      tier: 'any-file', wants: 'file', input_name: 'files', multi: true, running: false },
  ], refused: [], unprobed: [],
}, {})
ok('  more than one app reads as more than one, and both are rows',
   pMany.head === 'clip.mp4 \u2014 2 apps here would take it.' && pMany.rows.length === 2,
   pMany.head)
ok('  an app that takes any file does not pretend to have wanted a video',
   pMany.rows[1].line.indexOf('takes any file') === 0, pMany.rows[1].line)
ok('  and a file input that wants a list says so, on either branch \u2014 handing one '
   + 'path to a list input throws',
   /as a list/.test(pMany.rows[1].line), pMany.rows[1].line)
ok('  an app that is not running is said to be started, not left to be discovered',
   pMany.rows[0].note === 'not running \u2014 it will be started for this', pMany.rows[0].note)

const pNone = __fns.acceptsPanel({
  name: 'take.wav', kind: 'audio', fits: [], unprobed: ['Maestro'],
  refused: [
    { app: 'wan.git', app_name: 'wan', capability: 't2v',
      reason: 'it takes a video input, and this is an audio file' },
    { app: 'comfy.git', app_name: 'comfy', capability: 'run',
      reason: 'it wants a link, not a file on this machine' },
  ],
}, {})
ok('  nothing fits: said with the kind of file it is, rather than as an empty list',
   pNone.state === 'none'
   && pNone.head === 'Nothing installed here would take take.wav \u2014 it is an audio file.',
   pNone.head)
ok('  every app that said no is quoted with its own reason, so the refusal is '
   + 'readable rather than mysterious',
   pNone.notes.length === 3
   && pNone.notes[1].indexOf('it wants a link, not a file on this machine') > 0,
   pNone.notes.join(' | '))
ok('  an app that has never been probed is explained, and one app is one app',
   /^Maestro has not said what it can do yet/.test(pNone.notes[2])
   && /the first time an app is asked/.test(pNone.notes[2]), pNone.notes[2])
ok('  two unprobed apps are plural and are named',
   /^2 apps here have not said what they can do yet \(a, b\)/.test(
     __fns.acceptsPanel({ name: 'x.png', kind: 'image', fits: [], refused: [],
                          unprobed: ['a', 'b'] }, {}).notes[0]))

const pBare = __fns.acceptsPanel({ name: 'x.png', kind: 'image', fits: [], refused: [],
                                   unprobed: [] }, {})
ok('  and when nothing at all has an answer, nothing is invented',
   pBare.head === 'Nothing installed here takes a file at all yet.' && !pBare.notes.length,
   pBare.head)

ok('  file kinds are worded the way a person says them',
   __fns.kindWords('image') === 'an image' && __fns.kindWords('audio') === 'an audio file'
   && __fns.kindWords('model3d') === 'a 3D model' && __fns.kindWords('') === 'a file'
   && __fns.kindWords('probe') === 'a probe file',
   [__fns.kindWords('image'), __fns.kindWords('audio'), __fns.kindWords('model3d'),
    __fns.kindWords(''), __fns.kindWords('probe')].join(' | '))

// ------------------------------- a run's files, and what would take them on
// A pipeline is one job, and the bar has to say two different things: what came
// out, and — when a chain stopped short — exactly which stage stopped it. The
// stage numbers are in every sentence because "which step broke" is the only
// question a chain raises.

const mWork = __fns.madePanel({ state: 'running' }, null, {})
ok('  while it runs the bar says it will report, rather than showing an empty list',
   mWork.state === 'working' && /will say what it made/.test(mWork.head) && !mWork.rows.length,
   mWork.head)

const mOne = __fns.madePanel({
  state: 'done',
  result: { ok: true, files: ['C:/runs/one/cut.png'], stages: [
    { stage: 1, app: 'RMBG-2-Studio.git', intent: 'remove_background', ok: true,
      files: ['C:/runs/one/cut.png'] }] },
}, { ok: true, name: 'cut.png', kind: 'image', fits: [{
    app: 'comfy.git', app_name: 'comfy', capability: 'run', intent: 'run_a_workflow',
    tier: 'any-file', wants: 'file', input_name: 'files', multi: true, running: false,
  }], refused: [], unprobed: [] }, { app: 'RMBG-2-Studio' })
ok('  a finished run names what it made and calls the file by its own name',
   mOne.state === 'made' && mOne.head === 'RMBG-2-Studio made 1 file(s) \u2014 cut.png',
   mOne.head)
ok('  and the apps that would take it are the same rows as before \u2014 send-on is '
   + 'the routing answer, not a second one',
   mOne.rows.length === 1 && /takes any file/.test(mOne.rows[0].line), mOne.rows[0].line)
ok('  while the stage it took to get there is reported too', mOne.notes.length === 1
   && /^stage 1 · RMBG-2-Studio.git/.test(mOne.notes[0]) && /1 file\(s\)/.test(mOne.notes[0]),
   mOne.notes[0])

const mChain = __fns.madePanel({
  state: 'done',
  result: { ok: true, files: ['C:/runs/two/up.png'], stages: [
    { stage: 1, app: 'pretend', intent: 'cut', ok: true, files: ['C:/runs/one/cut.png'] },
    { stage: 2, app: 'second', intent: 'upscale', ok: true, files: ['C:/runs/two/up.png'] }] },
}, null, {})
ok('  a chain that ran says how many stages and what came out the far end',
   mChain.state === 'made' && mChain.head === 'the chain finished: 2 stages, and it made 1 file(s)',
   mChain.head)
ok('  every stage is listed, in order, with what it made',
   mChain.notes.length === 2 && /stage 1/.test(mChain.notes[0]) && /stage 2 · second/.test(mChain.notes[1]),
   mChain.notes.join(' | '))
const mNoTaker = __fns.madePanel({
  state: 'done',
  result: { ok: true, files: ['C:/runs/three/pack.zip'], stages: [
    { stage: 1, app: 'a', ok: true, files: ['C:/runs/three/pack.zip'] }] },
}, { ok: true, name: 'pack.zip', kind: 'archive', fits: [], unprobed: [],
  refused: [{ app: 'maestro', app_name: 'Maestro',
              reason: 'it takes an image input, and this is an archive' }] }, {})
ok('  a run whose output nothing here would take shows the routing sentence rather '
   + 'than an empty list of apps',
   mNoTaker.state === 'made' && !mNoTaker.rows.length
   && /Nothing installed here would take pack\.zip/.test(mNoTaker.next || ''), mNoTaker.next)

const mStop = __fns.madePanel({
  state: 'done',
  result: { ok: false, stopped_at: 2, files: [],
            error: 'stage 2 (second): the app ran out of memory',
            stages: [
              { stage: 1, app: 'pretend', intent: 'cut', ok: true, files: ['C:/runs/one/cut.png'] },
              { stage: 2, app: 'second', intent: 'upscale', ok: false,
                error: 'the app ran out of memory', files: [] }] },
}, null, {})
ok('  a chain that stopped names the stage that stopped it, and the reason the app gave',
   mStop.state === 'stopped'
   && mStop.head === 'the chain stopped at stage 2: stage 2 (second): the app ran out of memory',
   mStop.head)
ok('  and both stages are still reported, so the work that did happen is not lost',
   mStop.notes.length === 2 && /1 file\(s\)/.test(mStop.notes[0])
   && /the app ran out of memory/.test(mStop.notes[1]), mStop.notes.join(' | '))
ok('  a chain that never got going says the app that made nothing, rather than '
   + 'looking like it produced zero files on purpose',
   __fns.madePanel({
     state: 'done',
     result: { ok: false, stopped_at: 2, files: [],
               error: 'stage 1 produced no files, so stage 2 was not started',
               stages: [{ stage: 1, app: 'pretend', ok: true, files: [] }] },
   }, null, {}).state === 'stopped', 'stopped')

const mEmpty = __fns.madePanel({
  state: 'done',
  result: { ok: true, files: [], stages: [
    { stage: 1, app: 'pretend', ok: true, files: ['C:/runs/one/cut.png'] },
    { stage: 2, app: 'second', ok: true, files: [] }] },
}, null, {})
ok('  a chain that finished with nothing at the end says the last app made nothing '
   + 'to hand on \u2014 which is not the same as a failure, and not the same as silence',
   mEmpty.state === 'empty' && /the last app made nothing to hand on/.test(mEmpty.head),
   mEmpty.head)

const sendTree = guard('render the send bar', () => __fns.SendToApp({ ctx }))
if (sendTree) {
  const sendText = mkTexts(sendTree).join(' ')
  // One sentence, and no controls: the bar has nothing to offer until a file is
  // in hand, and offering a disabled Send button would be noise.
  ok('  with no file chosen the bar is one line telling the person how to pick one',
     /Right-click a file/.test(sendText) && !/Done|Sending/.test(sendText),
     sendText.slice(0, 140))
}

// --------------------------------------------------------- what runs on its own
// A repeat is the one thing here that fires while nobody is looking, so the
// states it can be shown in all have to be assertable: waiting, paused, and the
// one that matters most — a job gone from the store, which will never run again.
const rEmpty = __fns.repeatPanel({ ok: true, schedules: [], hermes_cron: true }, {})
ok('  with nothing scheduled, the repeats block says so in words rather than '
   + 'rendering an empty list',
   rEmpty.rows.length === 0 && /Nothing runs on its own yet/.test(rEmpty.empty),
   rEmpty.empty)

const rNoCron = __fns.repeatPanel({ ok: true, schedules: [], hermes_cron: false }, {})
ok('  a build with no cron store says that is why nothing can be scheduled, '
   + 'rather than showing an empty list',
   rNoCron.rows.length === 0 && /no cron store/.test(rNoCron.empty), rNoCron.empty)

const rRow = __fns.repeatPanel({ ok: true, note: 'scheduled runs fire while Hermes is running',
  schedules: [
    { id: 's1', app: 'RMBG-2-Studio.git', name: 'RMBG-2-Studio: remove_background',
      intent: 'remove_background', capability: 'cut', schedule: 'every day at 8am',
      state: 'waiting', next_run: '2026-10-15T08:00:00-04:00',
      runs: { runs: 3, last: 1 } },
    { id: 's2', app: 'comfy.git', intent: 'run_a_workflow', capability: 'run',
      schedule: 'every 6 hours', state: 'gone', next_run: null, runs: { runs: 0 },
      note: 'the cron job is not in the store any more, so this will not run' },
    { id: 's3', app: 'wan.git', intent: 'text_to_video', capability: 't2v',
      schedule: '0 7 * * 1', state: 'paused', next_run: '2026-10-19T07:00:00-04:00',
      runs: { runs: 1 } }] }, {})
ok('  a schedule reads as its time, its next fire and how often it has actually '
   + 'run — the app name without the .git the folder carries',
   rRow.head === '3 repeats' && rRow.rows[0].when === 'every day at 8am'
   && rRow.rows[0].next === '15/10 08:00' && rRow.rows[0].runs === 3
   && rRow.rows[0].app === 'RMBG-2-Studio', JSON.stringify(rRow.rows[0]))
ok('  a job that is gone from the store is shown as never firing again, and its '
   + 'own reason is carried through',
   rRow.rows[1].stateWords === 'will not run' && /will not run/.test(rRow.rows[1].note),
   rRow.rows[1].stateWords + ' / ' + rRow.rows[1].note)
ok('  a paused schedule says paused rather than looking like it will fire',
   rRow.rows[2].stateWords === 'paused' && rRow.rows[2].state === 'paused',
   rRow.rows[2].stateWords)
ok('  and the honest note about when these fire is kept with the block',
   rRow.notes.length === 1 && /while Hermes is running/.test(rRow.notes[0]),
   JSON.stringify(rRow.notes))
ok('  every row can be stopped, because that is the only thing to do to a schedule',
   rRow.rows.every((r) => r.canStop === true), JSON.stringify(rRow.rows.map((r) => r.canStop)))

const sTab = guard('render the runs tab with repeats', () => __fns.RunsTab({ ctx }))
if (sTab) {
  // The tab reads its runs over the network, so the first render is the reading
  // state — what can be asserted here is that the tab is the place the repeats
  // block is asked for, and that the block itself says where a repeat comes from
  // when there is none (the empty sentence is checked above, undiluted).
  const src = String(__fns.RunsTab)
  ok('  the runs tab is where the repeats block is asked for, so a schedule is '
     + 'found next to what has already run',
     /repeatPanel\(/.test(src) && /\/schedules/.test(src), src.slice(0, 120))
  ok('  and the empty sentence it shows is the one that says how to get a repeat',
     /every\s+morning/.test(rEmpty.empty), rEmpty.empty)
}

// ---- machines: a real OS, and the card that offers one --------------------
// A machine's whole surface is this tab, so what is pinned here is the wording
// that decides what a card offers and what a missing prerequisite says. The live
// half — making a distro, running a command inside it — is proven against the
// real wsl.exe in t_machines.py, where a browser would only be in the way.
const mMachine = (over) => Object.assign({ name: 'linux-box', provider: 'wsl',
   distro: 'athena-linux-box', image: {}, cpus: 4, memory_mb: 8192 }, over)
ok('  a running machine says so, and its verb is the one that undoes it',
   __fns.machineWords(mMachine({ state: 'running' })).chip === 'running'
   && __fns.machineWords(mMachine({ state: 'running' })).verb === 'Stop')
ok('  ... a stopped one offers Start, and one with no distro yet says exactly that',
   __fns.machineWords(mMachine({ state: 'stopped' })).verb === 'Start'
   && __fns.machineWords(mMachine({ state: 'missing' })).chip === 'no distro yet')
ok('  ... and a machine that is not there at all is not drawn as running or as broken',
   __fns.machineWords(null).chip === 'stopped' && __fns.machineWords({}).tone === '')
ok('  the line under the name says which OS it is and what it costs, not whether it works',
   __fns.machineLine(mMachine({})) === 'WSL · athena-linux-box · 4 cores · 8 GB',
   __fns.machineLine(mMachine({})))
ok('  ... an attached machine says whose it is, because a command in it sees real files',
   /your distro/.test(__fns.machineLine(mMachine({ attach: true }))))
ok('  ... and an image given as a file is named by the file, not by a whole path',
   /rootfs\.tar\.gz/.test(__fns.machineLine(mMachine({ image: { path: '/tmp/x/rootfs.tar.gz' } })))
   && !/\/tmp/.test(__fns.machineLine(mMachine({ image: { path: '/tmp/x/rootfs.tar.gz' } }))))
// ---- naming the machine, which is the first thing anyone does here ------------
// Reported: "making it a little more obvious where the user is supposed to type in the name of
// the machine. it's just greyed out really and took even me a minute to figure out why i couldn't
// click make it." The field had no label, its hint read like a value already in it, and the dead
// button said nothing. So what is pinned is that the field names itself, that the hint reads as
// an instruction, and that a button which cannot be pressed says why.
{
  const empty = __fns.newMachineRow({ name: '', preset: 'arch', onName() {}, onPreset() {},
                                      onSubmit() {} })
  const blanks = texts(empty)
  ok('  the name field is labelled, so it is not just a grey line to guess at',
     blanks.includes('Name'), JSON.stringify(blanks.slice(0, 6)))
  // The hint is the input's placeholder, which `texts` cannot see: it walks children, and a
  // placeholder is a prop. Read from the input itself, since that is the thing being judged.
  const propsOf = (n, test, out = []) => {
    if (Array.isArray(n)) { n.forEach((k) => propsOf(k, test, out)); return out }
    if (!n || typeof n !== 'object') return out
    if (n.props && test(n.props)) out.push(n.props)
    return propsOf(n.props && n.props.children, test, out)
  }
  const nameIn = propsOf(empty, (pr) => pr.className && /ah-namein/.test(pr.className))
  ok('  ... and its hint is an instruction rather than a value that looks already filled in',
     nameIn.length === 1 && /^type a name/.test(String(nameIn[0].placeholder))
     && /like linux-box/.test(String(nameIn[0].placeholder)),
     JSON.stringify(nameIn.map((pr) => pr.placeholder)))
  ok('  ... and its kind is labelled too, rather than being a bare dropdown',
     blanks.includes('Kind'))
  const btns = []
  const walk = (n) => {
    if (Array.isArray(n)) { n.forEach(walk); return }
    if (!n || typeof n !== 'object') return
    if (n.props && n.props.disabled !== undefined) btns.push(n.props)
    walk(n.props && n.props.children)
  }
  walk(empty)
  ok('  a make-it with no name is dead, and says what is missing instead of just sitting there',
     btns.length === 1 && btns[0].disabled === true
     && texts(empty).includes('give it a name first'), JSON.stringify(btns))
  const filled = __fns.newMachineRow({ name: 'omarchy', preset: 'arch', onName() {},
                                       onPreset() {}, onSubmit() {} })
  const btns2 = []
  const walk2 = (n) => {
    if (Array.isArray(n)) { n.forEach(walk2); return }
    if (!n || typeof n !== 'object') return
    if (n.props && n.props.disabled !== undefined) btns2.push(n.props)
    walk2(n.props && n.props.children)
  }
  walk2(filled)
  ok('  ... and the same button is live once it has one, with nothing left to explain',
     btns2.length === 1 && btns2[0].disabled === false
     && !texts(filled).includes('give it a name first'))
  // Enter in the field is the same as pressing the button: one name, one machine.
  let made = 0
  const enter = __fns.newMachineRow({ name: 'omarchy', preset: 'arch', onName() {},
                                      onPreset() {}, onSubmit: () => { made++ } })
  const inputs = []
  const walk3 = (n) => {
    if (Array.isArray(n)) { n.forEach(walk3); return }
    if (!n || typeof n !== 'object') return
    if (n.props && n.props.className && /ah-namein/.test(n.props.className)) inputs.push(n.props)
    walk3(n.props && n.props.children)
  }
  walk3(enter)
  ok('  ... and the field is the one the cursor starts in, because it is the only thing to do',
     inputs.length === 1 && inputs[0].autoFocus === true && inputs[0].value === 'omarchy')
  guard('  ... and Enter in it makes the machine without reaching for the mouse',
        () => { inputs[0].onKeyDown({ key: 'Enter' }) })
  ok('  ... once, not twice', made === 1, String(made))
}

// ---- qemu: the other half, where the image is somebody else's ------------------
// Same card, different machine: a QEMU guest boots an image instead of importing a
// distro, and nothing in it is reachable unless a port is forwarded. Both are
// pinned here because both are things a person decides, not things that happen.
const qMachine = (over) => Object.assign({ name: 'bazzite', provider: 'qemu', os: 'linux',
   image: { kind: 'iso', path: 'D:/iso/bazzite.iso' }, cpus: 6, memory_mb: 8192,
   ports: [{ host: 8080, guest: 80 }], ssh: { port: 2222 }, state: 'stopped' }, over)
ok('  a QEMU machine says which kind of image it boots — an installer is not a disk',
   /QEMU/.test(__fns.machineLine(qMachine({}))) && /iso bazzite\.iso/.test(__fns.machineLine(qMachine({}))),
   __fns.machineLine(qMachine({})))
ok('  ... and a disk image says it is a disk, because that one boots as it is',
   /disk arch\.qcow2/.test(__fns.machineLine(qMachine({ image: { kind: 'disk', path: '/x/arch.qcow2' } }))))
ok('  ... its ports are on the line, because they are the only way in from this computer',
   __fns.machineLine(qMachine({})).includes('2222\u219222')
   && __fns.machineLine(qMachine({})).includes('8080\u219280'))
ok('  ports typed the way a person types them become the pairs the spec wants',
   __fns.parsePorts('8080:80, 2222->22').length === 2
   && __fns.parsePorts('8080:80')[0].host === 8080 && __fns.parsePorts('8080:80')[0].guest === 80)
ok('  ... and junk is dropped rather than forwarded somewhere unexpected',
   __fns.parsePorts('nope, 0:80, 70000:80, 443:443').length === 1
   && __fns.parsePorts('').length === 0 && __fns.parsePorts(null).length === 0)
// ---- the image folder, and the menu drawn from it -------------------------
// Pointing one folder at the images is what makes "boot something of mine" a choice
// rather than a path somebody has to remember. What the menu *says* about each file
// is the kind the name suggests — labelled as a suggestion, corrected in the form.
const isoScan = { dir: 'D:/isos', exists: true, isos: [
   { name: 'win11.iso', path: 'D:/isos/win11.iso', kind: 'iso', size_mb: 6144 },
   { name: 'arch.qcow2', path: 'D:/isos/arch.qcow2', kind: 'disk', size_mb: 20480 },
   { name: 'mystery.bin', path: 'D:/isos/mystery.bin', kind: '', size_mb: 0.4 },
 ], skipped: 0 }
ok('  the menu is the files in the folder, and it always offers a way out of it',
   __fns.isoOptions(isoScan).length === 5
   && __fns.isoOptions(isoScan)[0].value === ''
   && __fns.isoOptions(isoScan)[4].value === '__other__',
   JSON.stringify(__fns.isoOptions(isoScan).map(o => o.value)))
ok('  ... and each file says what its name suggests it is, not just its filename',
   /win11\.iso \u2014 installer, 6\.0 GB/.test(__fns.isoOptions(isoScan)[1].label)
   && /arch\.qcow2 \u2014 disk image, 20\.0 GB/.test(__fns.isoOptions(isoScan)[2].label),
   __fns.isoOptions(isoScan)[1].label)
ok('  ... a file whose name suggests nothing is still listed, and says so',
   /kind not obvious/.test(__fns.isoOptions(isoScan)[3].label)
   && __fns.isoOptions(isoScan)[3].kind === '')
ok('  the folder line tells "no images yet" apart from "that folder is not there"',
   /2 images/.test(__fns.isoWords({ dir: 'D:/isos', exists: true, isos: [1, 2].map(() => ({})) }))
   && /not there yet/.test(__fns.isoWords({ dir: 'D:/isos', exists: false, isos: [] }))
   && !/images/.test(__fns.isoWords({ dir: 'D:/isos', exists: false, isos: [] })))
ok('  picking a file from the menu fills the path and the kind the name suggested',
   __fns.isoPatch(isoScan.isos[1]).imagePath === 'D:/isos/arch.qcow2'
   && __fns.isoPatch(isoScan.isos[1]).imageKind === 'disk')
ok('  ... and a Windows name only *suggests* a Windows guest, in a field that can be changed',
   __fns.isoPatch(isoScan.isos[0]).os === 'windows'
   && __fns.isoPatch(isoScan.isos[1]).os === undefined
   && __fns.isoPatch({}).imagePath === undefined && __fns.isoPatch(null).imageKind === undefined)
ok('  the size a guest is given is read off this computer, with the reason it gave',
   /for this computer: 8 cores \u00b7 15\.5 GB memory \u00b7 40 GB disk/.test(
     __fns.fitWords({ cpus: 8, memory_mb: 15872, disk_gb: 40, why: ['24 cores here \u2192 8 for the guest, leaving the host 16'] }))
   && /leaving the host 16/.test(__fns.fitWords({ cpus: 8, memory_mb: 15872, disk_gb: 40,
     why: ['24 cores here \u2192 8 for the guest, leaving the host 16'] })),
   __fns.fitWords({ cpus: 8, memory_mb: 15872, disk_gb: 40, why: ['24 cores here'] }))
ok('  ... and a fit that was never read says nothing rather than inventing numbers',
   __fns.fitWords(null) === '' && __fns.fitWords({}) === '')

// ---- subfolders, the folder you walk to, and the bar a download draws ----------
// A folder organised as windows/ linux/ mac/ must read as three short lists, and a
// folder somebody has to *type* is a folder they have to remember: the picker walks one
// level at a time and always knows which folder "Use this folder" means.
const isoNested = { dir: 'E:/isos', exists: true, folders: 3, isos: [
   { name: 'win11.iso', path: 'E:/isos/windows/win11.iso', rel: 'windows', kind: 'iso', size_mb: 6144 },
   { name: 'win10.iso', path: 'E:/isos/windows/win10.iso', rel: 'windows', kind: 'iso', size_mb: 5120 },
   { name: 'arch.iso', path: 'E:/isos/linux/arch/arch.iso', rel: 'linux/arch', kind: 'iso', size_mb: 1024 },
   { name: 'installer.iso', path: 'E:/isos/installer.iso', rel: '', kind: 'iso', size_mb: 2048 },
 ] }
ok('  images are grouped by the folder they are in, with the top folder first',
   __fns.isoGroups(isoNested).length === 3
   && __fns.isoGroups(isoNested)[0].rel === ''
   && __fns.isoGroups(isoNested)[0].label === 'in this folder'
   && __fns.isoGroups(isoNested)[1].rel === 'linux/arch'
   && __fns.isoGroups(isoNested)[2].rel === 'windows'
   && __fns.isoGroups(isoNested)[2].items.length === 2,
   JSON.stringify(__fns.isoGroups(isoNested).map(g => g.rel + ':' + g.items.length)))
ok('  ... and the folder travels with the file in the menu, because two install.iso in two folders are two files',
   __fns.isoOptions(isoNested)[1].label === 'windows/win11.iso \u2014 installer, 6.0 GB',
   __fns.isoOptions(isoNested)[1].label)
ok('  the folder line says how many folders the images were sorted into',
   /4 images in 3 folders/.test(__fns.isoWords(isoNested)), __fns.isoWords(isoNested))
ok('  ... and one folder of images is one folder, not "in 1 folders"',
   !/folders/.test(__fns.isoWords(isoScan)) && /3 images/.test(__fns.isoWords(isoScan)),
   __fns.isoWords(isoScan))
ok('  a list with nothing to group is empty rather than a phantom folder',
   __fns.isoGroups(null).length === 0 && __fns.isoGroups({}).length === 0
   && __fns.isoGroups({ isos: [{ name: 'x.iso' }] }).length === 0)
// Expanding and minimizing. A folder somebody filled for years is a wall of names, so a
// subfolder starts shut -- but the top folder starts *open*, because a loose ISO sitting
// in it is the common case and hiding it behind a click would be the worse default.
ok('  folders start shut for subfolders and open for the top one',
   __fns.isoGroupsShown(isoNested, {}).map(g => g.rel + ':' + g.open).join(' ') ===
   ':true linux/arch:false windows:false',
   JSON.stringify(__fns.isoGroupsShown(isoNested, {}).map(g => [g.rel, g.open])))
ok('  ... and opening one leaves the others exactly as they were',
   __fns.isoGroupsShown(isoNested, { windows: true }).filter(g => g.open).map(g => g.rel).join() === ',windows'
   && __fns.isoGroupsShown(isoNested, { windows: true }).map(g => g.count).join() === '1,1,2')
ok('  ... a folder that was shut on purpose stays shut, which is why this asks whether the key is there',
   __fns.isoGroupsShown(isoNested, { '': false }).filter(g => g.open).length === 0)
ok('  one button opens every folder, and the same button shuts every folder',
   Object.keys(__fns.groupMap(isoNested, true)).length === 3
   && Object.values(__fns.groupMap(isoNested, true)).every(v => v === true)
   && Object.values(__fns.groupMap(isoNested, false)).every(v => v === false)
   && Object.keys(__fns.groupMap(null, true)).length === 0)
// "Start did nothing" was a button that stopped a stopped machine and believed it.
ok('  the verb is the action, and it is read off the state rather than tried',
   __fns.machineWords(mMachine({ state: 'stopped' })).what === 'start'
   && __fns.machineWords(mMachine({ state: 'running' })).what === 'stop'
   && __fns.machineWords(mMachine({ state: 'missing' })).what === 'start'
   && __fns.machineWords(mMachine({ state: 'provisioning' })).what === '')
ok('  what a click says is missing comes back as a list either way',
   __fns.needList({ needs_setup: 'qemu', question: 'QEMU is not installed' }).length === 1
   && __fns.needList([{ needs_setup: 'qemu' }, { needs_setup: 'wsl' }]).length === 2
   && __fns.needList(null).length === 0 && __fns.needList('qemu').length === 0
   && __fns.needList([{ needs_setup: 'qemu' }, null, 'x']).length === 1)
ok('  the folder browser says where it is standing, and how much is in there',
   /2 folders, 3 files/.test(__fns.folderWords({ path: 'C:/isos', ok: true, dirs: [1, 2], files: 3 })),
   __fns.folderWords({ path: 'C:/isos', ok: true, dirs: [1, 2], files: 3 }))
ok('  ... an empty folder says it is empty, and one it cannot read says that instead',
   /0 folders/.test(__fns.folderWords({ path: 'C:/isos', ok: true, dirs: [] }))
   && /could not read/.test(__fns.folderWords({ path: 'C:/x', ok: false, error: 'could not read C:/x' }))
   && /Reading folders/.test(__fns.folderWords(null)))
// The bar itself. A download is the one install with real bytes in it, so the panel has
// to draw bytes — and a download whose size it never learned must not pretend to know one.
// The picker is a component, so render it: an empty listing from the harness's `rest`
// is the honest case here — it should draw "no folders inside this one", not throw.
let fpText = ''
guard('render the folder picker', () => {
  const t = []
  strings(__fns.FolderPicker({ ctx, start: 'C:/isos', busy: '', onUse: () => {}, onClose: () => {} }), t)
  fpText = t.join(' | ')
})
const barText = (p) => { const t = []; strings(__fns.JobProgress({ progress: p }), t); return t.join(' | ') }
const dlProg = { done: 50000000, total: 206615928, label: 'downloading qemu-w64-setup.exe' }
ok('  a download being watched draws the bytes, and the name of what is coming down',
   /48 MB \/ 197 MB/.test(barText(dlProg)) && /downloading qemu-w64-setup\.exe/.test(barText(dlProg)),
   barText(dlProg))
ok('  ... and a download whose size is unknown shows what arrived and divides by nothing',
   !/\//.test(barText({ done: 50000000, total: 0, label: 'downloading x' })),
   barText({ done: 50000000, total: 0, label: 'downloading x' }))
ok('  ... and no progress at all draws nothing rather than an empty bar',
   __fns.JobProgress({ progress: null }) === null)
ok('  the folder picker offers a way up, a way in, and a way out',
   /Use this folder/.test(fpText) && /Cancel/.test(fpText)
   && /Only folders/.test(fpText), fpText.slice(0, 240))

ok('  a QEMU machine still offers to start — the setup card is what says QEMU is missing',
   __fns.machineWords(qMachine({})).verb === 'Start' && __fns.machineWords(qMachine({})).chip === 'stopped')

const qCard = guard('render the qemu card', () => __fns.MachineCard({ ctx: {},
   m: qMachine({}), words: __fns.machineWords(qMachine({})), open: true, onOpen: () => {},
   menuOpen: false, onMenu: () => {}, onAct: () => {}, onExec: () => {}, busy: '', out: '',
   cmd: '', onCmd: () => {} }))
if (qCard) {
   const say = texts(qCard).join(' ')
   ok('  a QEMU card offers how to reach it, not a command box that would refuse',
      /does not share your localhost/.test(say) && /2222/.test(say), say.slice(0, 160))
   ok('  ... and it names the host-side address, because that is what gets opened',
      /127\.0\.0\.1/.test(say))
   ok('  ... and the verb is still the one that starts it',
      /Start/.test(say))
}

ok('  a missing prerequisite arrives as a question carrying the command, not as a failure',
   __fns.needWords({ needs_setup: 'wsl', question: 'WSL is not installed. Set it up now?',
     command: 'wsl --install --no-distribution' }).command === 'wsl --install --no-distribution')
ok('  ... and that command is passed through as the backend worded it, never invented here',
   __fns.needWords({ needs_setup: 'qemu', command: 'winget install -e --id X' }).command
     === 'winget install -e --id X'
   && __fns.needWords({}).command === '')

// ---- the wait at launch --------------------------------------------------------
// A cold engine is about six seconds of a port that accepts and says nothing. Two
// things made that feel like ten to thirty: a fixed five-second clock, and a header
// that called a boot in progress "stopped".
ok('  a boot in progress is watched every second; a settled engine waits the long clock',
   __fns.pollDelay(null) === 1000 && __fns.pollDelay({ ready: false, starting: true }) === 1000
   && __fns.pollDelay({ ready: false, running: true }) === 1000
   && __fns.pollDelay({ ready: true }) === 5000,
   String(__fns.pollDelay({ ready: true })))
ok('  ... and the header calls that boot starting, not stopped',
   __fns.engineLine({ ready: false, starting: true }, '') === 'starting\u2026'
   && __fns.engineLine({ ready: false, running: true }, '') === 'starting\u2026'
   && __fns.engineLine(null, '') === 'stopped'
   && __fns.engineLine({ ready: false, last_error: 'no compatible node' }, '') === 'no compatible node',
   __fns.engineLine({ ready: false, starting: true }, ''))

ok('  ... and says how long the wait has been, since "a while" is not a number',
   __fns.engineLine({ ready: false, starting: true, starting_ms: 14000 }, '') === 'starting\u2026 14s'
   && __fns.engineLine({ ready: false, starting: true, starting_ms: 45000 }, '') === 'starting\u2026 45s'
   && __fns.engineLine({ ready: false, running: true, starting_ms: 2200 }, '') === 'starting\u2026 2s',
   __fns.engineLine({ ready: false, starting: true, starting_ms: 14000 }, ''))

ok('  ... and invents no number it was never given',
   __fns.engineLine({ ready: false, starting: true }, '') === 'starting\u2026'
   && __fns.engineLine({ ready: false, starting: true, starting_ms: null }, '') === 'starting\u2026'
   && __fns.engineLine({ ready: false, starting: true, starting_ms: 900 }, '') === 'starting\u2026',
   __fns.engineLine({ ready: false, starting: true, starting_ms: null }, ''))

ok('  ... rounding to seconds, and saying nothing below one',
   __fns.waited(2500) === ' 3s' && __fns.waited(1000) === ' 1s' && __fns.waited(999) === ''
   && __fns.waited(undefined) === '' && __fns.waited(NaN) === '' && __fns.waited('14000') === '',
   __fns.waited(2500))
ok('  ... and a live one says ready with its port, with the home path folded away',
   /ready \u00b7 port 42001/.test(__fns.engineLine({ ready: true, port: 42001,
     home: 'C:/x/AppData/Local/hermes/athena' }, ''))
   && __fns.engineLine({ ready: true, port: 42001,
     home: 'C:/x/AppData/Local/hermes/athena' }, '').includes('\u2026/athena')
   && __fns.engineLine({ ready: true, port: 42001, home: '' }, '') === 'ready \u00b7 port 42001 \u00b7 ',
   __fns.engineLine({ ready: true, port: 42001, home: 'C:/x/AppData/Local/hermes/athena' }, ''))

const needCard = guard('render the setup card', () => __fns.SetupCard({
   need: { needs_setup: 'wsl', question: 'WSL is not installed. Set it up now?',
     detail: 'Windows needs the feature enabled.', command: 'wsl --install --no-distribution',
     then: 'Reboot if Windows asks, then come back.' },
   onInstall: () => {}, busy: '' }))
if (needCard) {
   const ask = texts(needCard).join(' ')
   ok('  the setup card asks the question before it offers to do anything',
      /Set it up now/.test(ask) && /wsl --install --no-distribution/.test(ask), ask.slice(0, 140))
   ok('  ... and it says what comes after, because a reboot is not a failure',
      /Reboot if Windows asks/.test(ask))
}

const mCard = guard('render a machine card', () => __fns.MachineCard({ ctx,
   m: mMachine({ state: 'running' }), words: __fns.machineWords(mMachine({ state: 'running' })),
   open: false, menuOpen: false, onOpen: () => {}, onMenu: () => {}, onAct: () => {},
   onExec: () => {}, busy: '', out: '', cmd: '', onCmd: () => {} }))
if (mCard) {
   const shown = texts(mCard).join(' ')
   ok('  the card offers the machine\'s verb and the console, and nothing else shouts',
      /Stop/.test(shown) && /Run a command/.test(shown), shown.slice(0, 140))
   ok('  ... and a stopped machine offers Start instead', (() => {
      const t = texts(__fns.MachineCard({ ctx, m: mMachine({ state: 'stopped' }),
         words: __fns.machineWords(mMachine({ state: 'stopped' })), open: false, menuOpen: false,
         onOpen: () => {}, onMenu: () => {}, onAct: () => {}, onExec: () => {}, busy: '',
         out: '', cmd: '', onCmd: () => {} })).join(' ')
      return /Start/.test(t) && !/Stop/.test(t)
   })())
   const mMenu = guard('render a machine card with its menu open', () => __fns.MachineCard({ ctx,
      m: mMachine({}), words: __fns.machineWords(mMachine({})), open: false, menuOpen: true,
      onOpen: () => {}, onMenu: () => {}, onAct: () => {}, onExec: () => {}, busy: '',
      out: '', cmd: '', onCmd: () => {} }))
   if (mMenu) {
      const items = texts(mMenu).join(' ')
      ok('  the menu keeps destroying a distro behind the dangerous wording',
         /Destroy/.test(items) && /Forget/.test(items), items.slice(0, 140))
   }
}

// The panel's screen: the numbers that decide whether a click lands where it looks like it
// lands, whether the tab keeps asking, and how long it waits in between.
const gp = __fns.guestPoint
ok('  a click in the middle of a scaled picture is the middle of the guest\'s screen',
   gp({ left: 0, top: 0, width: 640, height: 400 }, 320, 200, 1280, 800).x === 640 &&
   gp({ left: 0, top: 0, width: 640, height: 400 }, 320, 200, 1280, 800).y === 400)
ok('  ... and a click on the edge is the edge, never off it',
   gp({ left: 10, top: 20, width: 300, height: 150 }, 10, 20, 1280, 800).x === 0 &&
   gp({ left: 10, top: 20, width: 300, height: 150 }, 9999, 9999, 1280, 800).x === 1279 &&
   gp({ left: 10, top: 20, width: 300, height: 150 }, 9999, 9999, 1280, 800).y === 799)
ok('  a scaled-up picture still sends the guest its own coordinates',
   gp({ left: 0, top: 0, width: 2560, height: 1600 }, 1280, 800, 1280, 800).x === 640)

// A guest that is a different shape from the pane is drawn letterboxed -- the picture is
// centred in the element with empty space above and below it, because `object-fit: contain`
// keeps the guest's shape. Dividing by the element's own box read every click below that
// margin as further down the screen than it was, by exactly the size of the margin.
const _lb = { left: 0, top: 0, width: 800, height: 600 }   // 1.33 box, 1.78 guest
ok('  a click on a letterboxed picture is measured against the picture, not the box',
   gp(_lb, 400, 300, 1920, 1080).y === 540 && gp(_lb, 400, 100, 1920, 1080).y === 60,
   `${gp(_lb, 400, 100, 1920, 1080).y}`)
ok('  ... and the sideways margin is ignored the same way',
   gp({ left: 0, top: 0, width: 400, height: 400 }, 100, 200, 1920, 1080).x === 480,
   `${gp({ left: 0, top: 0, width: 400, height: 400 }, 100, 200, 1920, 1080).x}`)
ok('  ... while a click in the empty margin stays on the screen instead of going negative',
   gp(_lb, 400, 5, 1920, 1080).y === 0 && gp(_lb, 400, 595, 1920, 1080).y === 1079)
ok('  a picture that already matches the pane is unchanged by any of that',
   gp({ left: 0, top: 0, width: 640, height: 360 }, 320, 180, 1920, 1080).x === 960 &&
   gp({ left: 0, top: 0, width: 640, height: 360 }, 320, 180, 1920, 1080).y === 540)

const sw = __fns.screenWords
ok('  the line above the picture says its size, weight and when it was read',
   sw({ width: 1280, height: 800, bytes: 4096, at: '2026-10-14T21:03:12',
        image: 'data:image/jpeg;base64,x' }, true) === '1280×800 · 4 KB · 21:03:12')
ok('  a machine that is not running says so instead of showing a stale number',
   sw({}, false) === 'not running' && sw({ note: 'it is not running' }, false) === 'it is not running')
ok('  a frame that failed says why rather than looking like a blank screen',
   sw({ error: 'nothing is listening on 127.0.0.1:5900' }, true).indexOf('5900') > -1)

const sl = __fns.screenLive
ok('  the tab asks for frames only while a screen is open on a running machine',
   sl('panel', true, true) && !sl('panel', false, true) && !sl('panel', true, false) &&
   !sl('none', true, true))

const fd = __fns.frameDelay
ok('  it asks again quickly while the guest\'s screen is changing',
   fd({ image: 'data:image/jpeg;base64,x', bytes: 900 }, 0) === 1200)
ok('  ... and slows down once two frames in a row were byte-identical',
   fd({ image: 'data:image/jpeg;base64,x', bytes: 900 }, 2) === 2500)
ok('  a screen that could not be read is not polled like a live one',
   fd({ error: 'no route to host' }, 0) === 2000)

ok('  a shifted character is sent as itself, so the guest does the shifting',
   __fns.screenKey({ key: 'A' }) === 'A' && __fns.screenKey({ key: '@' }) === '@' &&
   __fns.screenKey({ key: 'ArrowUp' }) === 'ArrowUp' && __fns.screenKey({ key: '' }) === '')

const mScreenCard = guard('render a machine card with the screen controls', () => __fns.MachineCard({ ctx,
   m: qMachine({ state: 'running', display: 'panel' }),
   words: __fns.machineWords(qMachine({ state: 'running' })), open: true, menuOpen: false,
   onOpen: () => {}, onMenu: () => {}, onAct: () => {}, onExec: () => {}, busy: '', out: '',
   cmd: '', onCmd: () => {}, onWatch: () => {}, onPopOut: () => {} }))
if (mScreenCard) {
   const shown = texts(mScreenCard).join(' ')
   ok('  a machine card offers its screen as somewhere to go, and a way out to a window',
      /Watch its screen/.test(shown) && /Open in a new window/.test(shown) &&
      !/Show the screen/.test(shown), shown.slice(0, 200))
   const win = texts(__fns.MachineCard({ ctx, m: qMachine({ state: 'running', display: 'window' }),
      words: __fns.machineWords(qMachine({ state: 'running' })), open: true, menuOpen: false,
      onOpen: () => {}, onMenu: () => {}, onAct: () => {}, onExec: () => {}, busy: '', out: '',
      cmd: '', onCmd: () => {}, onWatch: () => {}, onPopOut: () => {} })).join(' ')
   ok('  a machine with a window of its own offers to come back into the panel',
      /Bring it back into the panel/.test(win) && !/Open in a new window/.test(win), win.slice(0, 200))
}

// A machine you are watching belongs in the tab bar, beside the workbench it came from --
// like an app's output, and not as a thumbnail inside the card.
const tabBars = guard('the tab bar holds a machine that is being watched', () => ({
   none: __fns.shellTabs(null, null, null).map((x) => x[0]).join(','),
   watch: __fns.shellTabs(null, null, 'OmarchyVM'),
   both: __fns.shellTabs('some-app', null, 'OmarchyVM'),
}))
if (tabBars) {
   ok('  nothing watched, nothing extra in the bar',
      tabBars.none === 'apps,store,files,machines,viewer,runs,settings', tabBars.none)
   const names = tabBars.watch.map((x) => x[0])
   ok('  a watched machine gets a tab of its own, named after it',
      names.includes('mscreen') && tabBars.watch.find((x) => x[0] === 'mscreen')[1] === 'OmarchyVM'
      && names.indexOf('mscreen') < names.indexOf('settings'), names.join(','))
   ok('  ... and it does not disturb the terminal tab when both are up',
      tabBars.both.map((x) => x[0]).join(',') === 'apps,store,files,machines,viewer,mscreen,terminal,runs,settings',
      tabBars.both.map((x) => x[0]).join(','))
}

const mScreenTab = guard('render a machine screen tab', () => {
   __fns.watchMachine.set('OmarchyVM')
   const out = __fns.MachineScreenTab({ ctx })
   __fns.watchMachine.set(null)
   return out
})
if (mScreenTab) {
   const seen = texts(mScreenTab).join(' ')
   ok('  the screen tab says which machine it is showing', /OmarchyVM/.test(seen), seen.slice(0, 200))
   ok('  ... and that its picture goes to the guest, at any size',
      /reach the guest|goes to the guest/.test(seen), seen.slice(0, 200))
}

// Typing felt slow on a machine that was not slow: measured here, a keystroke leaves in ~20 ms
// and the guest has drawn the answer in ~139 ms, while the panel asked for a picture every 1.2 s
// and then every 2.5 s. Input now buys a fast cadence for a moment, and the guest's own
// behaviour decides after that.
const cadence = guard('read the screen at the speed of what just happened', () => ({
   justTyped: __fns.frameDelay({ image: 'x' }, 9, 0),
   midBurst: __fns.frameDelay({ image: 'x' }, 9, 1500),
   afterBurst: __fns.frameDelay({ image: 'x' }, 9, 2000),
   moving: __fns.frameDelay({ image: 'x' }, 0, undefined),
   still: __fns.frameDelay({ image: 'x' }, 2, undefined),
   broken: __fns.frameDelay({ error: 'gone' }, 0, 0),
}))
if (cadence) {
   const fast = __fns.INPUT_FRAME_MS
   ok('  a keystroke asks for the picture back in the guest\'s own time, not the panel\'s habit',
      cadence.justTyped === fast && cadence.midBurst === fast && fast <= 250
      && __fns.INPUT_ECHO_MS <= 150, JSON.stringify(cadence))
   ok('  ... and the fast cadence ends on its own, so a quiet machine is not read 5 times a second',
      cadence.afterBurst === __fns.STILL_FRAME_MS && cadence.still === __fns.STILL_FRAME_MS
      && cadence.moving === __fns.MOVING_FRAME_MS, JSON.stringify(cadence))
   ok('  ... and a screen that is not there yet is still waited on patiently',
      cadence.broken === 2000 && fast < __fns.MOVING_FRAME_MS
      && __fns.MOVING_FRAME_MS < __fns.STILL_FRAME_MS, JSON.stringify(cadence))
}

// An install finishes, says "reboot now", and a machine that starts on its own installer again
// looks like an install that was lost. The default is the disk first; the ISO-first order is the
// one that has to be asked for, and the tab says which way it is in words.
const boot = guard('decide what a machine with a medium attached starts on', () => ({
   iso_default: __fns.bootChoice({ provider: 'qemu', image: { kind: 'iso' }, boot: '' }),
   iso_medium: __fns.bootChoice({ provider: 'qemu', image: { kind: 'iso' }, boot: 'medium' }),
   disk: __fns.bootChoice({ provider: 'qemu', image: { kind: 'disk' }, boot: '' }),
   wsl: __fns.bootChoice({ provider: 'wsl' }),
   nothing: __fns.bootChoice(null),
   adopted: __fns.bootChoice({ provider: 'qemu', image: { kind: 'iso' }, boot: 'system',
                               installed: true }),
}))
if (boot) {
   ok('  a machine with an installer ISO attached says its disk comes first',
      !!boot.iso_default && boot.iso_default.next === 'Start on the installer'
      && boot.iso_default.now.includes('disk'), JSON.stringify(boot.iso_default))
   ok('  ... and one asked to start on the medium every time says so, and offers the way back',
      !!boot.iso_medium && boot.iso_medium.next === 'Start on its own system'
      && boot.iso_medium.now.includes('every time'), JSON.stringify(boot.iso_medium))
   ok('  ... and a machine whose disk holds a system says so, and offers the installer back',
      !!boot.adopted && boot.adopted.now.includes('installed system')
      && boot.adopted.next === 'Attach the installer again', JSON.stringify(boot.adopted))
   ok('  ... while a machine with no medium attached is offered nothing at all',
      boot.disk === null && boot.wsl === null && boot.nothing === null, JSON.stringify(boot))
}

// A screen that was moved into a window of its own is not this pane's picture: reading it
// anyway was a full frame over VNC every 1.2 s for something nobody here is looking at.
const live = guard('decide whether this pane reads a screen', () => ({
   running_panel: __fns.screenLive('panel', true, true),
   running_window: __fns.screenLive('window', true, true),
   running_none: __fns.screenLive('none', true, true),
   stopped_panel: __fns.screenLive('panel', false, true),
   hidden_tab: __fns.screenLive('panel', true, false),
   no_opinion: __fns.screenLive('', true, true),
}))
if (live) {
   ok('  a running machine whose screen is in the panel is read',
      live.running_panel === true && live.no_opinion === true, JSON.stringify(live))
   ok('  ... and one whose screen is a window of its own, or has none, is not',
      live.running_window === false && live.running_none === false, JSON.stringify(live))
   ok('  ... and a stopped machine, or a tab nobody is looking at, is not either',
      live.stopped_panel === false && live.hidden_tab === false, JSON.stringify(live))
}

// --- the tab that owns the read loop, mounted ----------------------------------
// This is the check for the reported crash: "plugin-workspace:athena-window failed to render --
// liveRef is not defined". The kick the picture calls moves refs that belong to the tab's read
// loop, and when the loop and the names end up in different functions the tab throws as it
// mounts and takes the whole workspace down with it. Every other check in this file reads the
// source; this one runs the component, so a name used where it is not declared fails here
// instead of passing as a string that merely occurs somewhere in the file.
{
  const before = __fns.watchMachine.get()
  __fns.watchMachine.set('rig')
  const mctx = {
    rest: async (p) => (p === '/machines'
      ? { machines: [{ name: 'rig', provider: 'qemu', state: 'running', display: 'panel',
                       image: { kind: 'disk', path: 'arch.qcow2' }, cpus: 6, memory_mb: 8192 }] }
      : { image: 'data:image/jpeg;base64,x', width: 640, height: 480, bytes: 4096 }),
    storage: { get: (k, def) => def, set: () => {} },
    onDispose: () => {}, host: {}, i18n: { register() {} }, register: () => () => {},
  }
  __effects.splice(0)
  const tree = guard('  a machine screen tab mounts and draws', () => __fns.MachineScreenTab({ ctx: mctx }))
  ok('  ... and it drew something rather than nothing', !!tree)
  const fx = __effects.splice(0)
  ok('  ... and it asked for the machine and the picture, not just one of them',
     fx.length >= 2, `${fx.length} effect(s)`)
  guard('  ... and the read loop inside it runs', () => { fx.forEach((f) => f()) })
  __fns.watchMachine.set(before)
}

// --- typing without a click in front of every letter --------------------------
// This is the reported setup problem, and it is not a thing a source check can prove:
// the picture was the only listener, so it needed focus, and the person was clicking before
// every keystroke. So: a key aimed at the picture reaches the guest, exactly once, even though
// two handlers see it (React's synthetic event, and the tab's native window listener). And the
// kindnesses around it: a field being typed into keeps its keys, a key that is not the
// picture's business is left alone, and the app's own Ctrl-shortcuts are not swallowed.
const listens = {}
const realWindow = globalThis.window
globalThis.window = {
  ...realWindow, innerWidth: 1600, innerHeight: 900,
  addEventListener: (t, f) => { (listens[t] = listens[t] || []).push(f) },
  removeEventListener: (t, f) => { listens[t] = (listens[t] || []).filter((x) => x !== f) },
}
const cleanups = []
const sent = []
const kctx = { rest: async (p, o) => { sent.push({ p, body: (o && o.body) || null }); return {} } }
const keyEvent = (key, extra = {}) => ({
  key, target: null, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
  preventDefault() {}, ...extra,
})
try {
  __effects.splice(0)
  __fns.GuestScreen({ ctx: kctx, name: 'rig', kick: () => {},
                      frame: { image: 'data:image/jpeg;base64,x', width: 640, height: 480 },
                      onWarn: () => {} })
  const fx = __effects.splice(0)
  fx.forEach((f) => { const c = f(); if (typeof c === 'function') cleanups.push(c) })
  ok('  a guest picture asks the window for keys as soon as it is drawn',
     (listens.keydown || []).length === 1, `${(listens.keydown || []).length} listener(s)`)

  /* Input goes out through a queue -- the request itself, the branch that notices it landed, the
     drain of whatever arrived while it was out, and the request that drain makes -- so a single
     `await` is not long enough to call it finished. Eight microtasks is the whole chain, and
     waiting for the chain is the difference between testing the queue and testing the tick. */
  const settle = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve() }
  const press = async (e) => {
    for (const f of (listens.keydown || [])) f(e)
    await settle()
  }

  // Reported: "keyboard input still laggy." Each keystroke used to clear the pending frame timer
  // and re-arm it from the clock, so a person typing faster than INPUT_ECHO_MS postponed the one
  // call that reads a frame until they stopped: the picture went stale exactly while they were
  // typing into it. The rule now is that a look already due is never pushed later.
  const clock = 1000000
  ok('  typing never pushes a look further away than it already was',
     __fns.kickDelay(0, clock) === __fns.INPUT_ECHO_MS
     && __fns.kickDelay(clock + 30, clock) === null                 // due sooner: leave it alone
     && __fns.kickDelay(clock + 200, clock) === __fns.INPUT_ECHO_MS  // further off: bring it in
     && __fns.kickDelay(clock - 5, clock) === __fns.INPUT_ECHO_MS,   // spent: arm a fresh one
     JSON.stringify([__fns.kickDelay(0, clock), __fns.kickDelay(clock + 30, clock),
                     __fns.kickDelay(clock + 200, clock), __fns.kickDelay(clock - 5, clock)]))
  // The picture holds the keyboard (opening the tab focuses it, so no click is needed to
  // begin), and a key aimed at it gets through.
  const onPicture = { closest: (sel) => (sel === '.ah-screendock' ? {} : null) }
  await press(keyEvent('a', { target: onPicture }))
  ok('  a keystroke aimed at the picture reaches the guest',
     sent.length === 1 && sent[0].p === '/machines/rig/screen/input'
     && JSON.stringify(sent[0].body) === JSON.stringify({ events: [{ kind: 'key', key: 'a' }] }),
     JSON.stringify(sent))

  // A key that belongs to the rest of the app is not the guest's: without this, a letter typed
  // into the chat with this tab open would be typed into the guest as well.
  sent.length = 0
  await press(keyEvent('z'))
  ok('  a key that is not aimed at the picture is left alone by it', sent.length === 0,
     JSON.stringify(sent))

  sent.length = 0
  await press(keyEvent('a', { target: { closest: () => ({}) } }))   // a field of this app
  ok('  text being typed into a field of this app stays in that field', sent.length === 0,
     JSON.stringify(sent))
  await press(keyEvent('k', { ctrlKey: true }))
  ok("  the app's own shortcuts are not swallowed by the guest", sent.length === 0,
     JSON.stringify(sent))

  // Reported: "Omarchy keyboard shortcuts act on Hermes instead of the guest." Super is the key
  // that desktop's own shortcuts live on, and it used to be dropped by the window listener --
  // and, when the picture handled the stroke itself, sent as a bare key with the modifier
  // stripped off, which the guest acts on as a different keystroke entirely.
  sent.length = 0
  await press(keyEvent('Enter', { target: onPicture, metaKey: true }))
  ok('  a Super combination reaches the guest as Super, not as the bare key it was held with',
     sent.length === 1 && JSON.stringify(sent[0].body)
       === JSON.stringify({ events: [{ kind: 'combo', mods: ['Super'], key: 'Enter' }] }),
     JSON.stringify(sent))

  sent.length = 0
  await press(keyEvent('e', { target: onPicture, metaKey: true, shiftKey: true }))
  ok('  ... and every modifier held travels with it, in the order it was held',
     sent.length === 1 && JSON.stringify(sent[0].body)
       === JSON.stringify({ events: [{ kind: 'combo', mods: ['Super', 'Shift'], key: 'e' }] }),
     JSON.stringify(sent))

  sent.length = 0
  await press(keyEvent('k', { ctrlKey: true, metaKey: true }))
  ok("  ... while a Super combination aimed at the app is still the app's", sent.length === 0,
     JSON.stringify(sent))

  ok('  ... and the dock promises Super in so many words',
     /Super\+Enter go to the VM/.test(String(__fns.dockNotes(true).key)),
     String(__fns.dockNotes(true).key))

  // The picture's own handler still sends the combinations, and the two do not both fire.
  const img = __fns.GuestScreen({ ctx: kctx, name: 'rig', kick: () => {},
                                  frame: { image: 'data:image/jpeg;base64,x', width: 640, height: 480 },
                                  onWarn: () => {} })
  // Functions do not survive JSON.stringify, so read the node itself: a picture you can
  // click into is what sends a combination, and that is the half the window must not take.
  const imgNode = (img.props.children || [])[0] || {}
  ok('  the picture is still focusable, for the combinations a window must not take',
     imgNode.props && imgNode.props.tabIndex === 0
     && typeof imgNode.props.onKeyDown === 'function',
     JSON.stringify(Object.keys((imgNode.props) || {})))
  // The reported "it resets into our session" was the app's type-to-focus reflex: a printable
  // key on ordinary chrome focuses the chat composer. Focus inside a `[data-terminal]` scope
  // keeps its keys -- that is the marker Hermes' own remote-screen pane uses -- so the picture
  // and its host both carry it, plus the sibling marker that keeps the close-tab router away.
  ok('  ... and it claims the keyboard scope, so the chat cannot take a letter back',
     imgNode.props['data-terminal'] === '' && imgNode.props['data-remote-screen'] === '',
     JSON.stringify({ img: imgNode.props['data-terminal'],
                      host: (img.props || {})['data-terminal'] }))

  // Plugin panes run in the app's React singleton, so the picture's own handler is handed a
  // *synthetic* event while the tab's window listener sees the native one underneath it. A
  // marker on one of them is not a marker on the other -- that is how "zack" arrived as
  // "zzaacckk", one character per event twice over.
  sent.length = 0
  const nativeKey = keyEvent('a', { target: onPicture })
  const synthetic = { ...nativeKey, nativeEvent: nativeKey }
  imgNode.props.onKeyDown(synthetic)
  await press(nativeKey)
  ok('  one keypress is one character, even when React and the tab both see it',
     sent.length === 1, `${sent.length} sent: ${JSON.stringify(sent.map((x) => x.body))}`)

  /* --- getting back out ------------------------------------------------------
     The pointer is never captured (probe_tabs checks the source for that), so the mouse can
     always leave the picture. The keyboard IS held while this tab is open -- that is what makes
     typing work with no click to begin it -- so it needs a way out that does not depend on the
     mouse: the Ctrl+Alt gesture, and a button for the person whose mouse is the thing that
     still works. */
  const notesOn = __fns.dockNotes(true), notesOff = __fns.dockNotes(false)
  ok('  the dock says who holds the keyboard and how to take it back',
     /Ctrl\+Alt/.test(notesOn.how) && /keyboard is back on this desktop/.test(notesOff.key)
     && __fns.keyboardButton(true) === 'Release keyboard'
     && __fns.keyboardButton(false) === 'Take keyboard',
     JSON.stringify([notesOn.how, notesOff.key]))
  ok('  ... and promises, in both states, that the pointer is never captured',
     /never captured/.test(notesOn.mouse) && /never captured/.test(notesOff.mouse),
     notesOn.mouse)
  ok('  ... in one line, so there is one place to read it',
     /Ctrl\+Alt/.test(__fns.keyboardNote(true))
     && /hand it to the VM/.test(__fns.keyboardNote(false)),
     __fns.keyboardNote(true))

  /* --- who is actually driving ------------------------------------------------
     The dock line above is about where this browser's keys go. This one is about the guest's
     single keyboard, which is a different question and the one that had no answer: an agent
     typing installer answers and a person clicking the same screen are two hands on it, and
     until the frame carried a lease nobody could tell -- or refuse -- the difference. The
     panel draws the backend's answer and owns the one control that changes it. */
  ok('  nobody driving draws no lease line at all',
     __fns.leaseWords(null, 'rig') === null && __fns.leaseWords(undefined, 'rig') === null
     && __fns.leaseWords({}, 'rig') === null,
     JSON.stringify([__fns.leaseWords(null, 'rig'), __fns.leaseWords({}, 'rig')]))
  const lwAgent = __fns.leaseWords({ who: 'agent', for_seconds: 12.4 }, 'rig')
  ok('  an agent driving says so, says how long it has had it, and carries the way out',
     !!lwAgent && lwAgent.who === 'agent' && /An agent is driving/.test(lwAgent.text)
     && /12s/.test(lwAgent.text) && lwAgent.button === 'Take it back', JSON.stringify(lwAgent))
  ok('  ... and a hold with no length claimed is not given one',
     !/undefined|NaN/.test(__fns.leaseWords({ who: 'agent' }, 'rig').text),
     __fns.leaseWords({ who: 'agent' }, 'rig').text)
  const lwPerson = __fns.leaseWords({ who: 'person' }, 'rig')
  ok('  a person driving gets no button -- there is nothing to take back from them',
     !!lwPerson && lwPerson.button === '' && /driving this screen/.test(lwPerson.text)
     && /refused/.test(lwPerson.text), JSON.stringify(lwPerson))
  ok("  ... and the tooltip names the machine, for a tab left open on another one",
     /rig/.test(String(__fns.leaseWords({ who: 'agent' }, 'rig').title))
     && /rig/.test(String(__fns.leaseWords({ who: 'person' }, 'rig').title)),
     String(__fns.leaseWords({ who: 'agent' }, 'rig').title))

  // Drawn, and clickable: the row only means anything if its button reaches the route.
  const leaseCalls = []
  const lctx = { rest: async (p, o) => { leaseCalls.push({ p, o: o || {} }); return { ok: true } } }
  const lg = __fns.GuestScreen({ ctx: lctx, name: 'rig', kick: () => {}, onWarn: () => {},
    frame: { image: 'data:image/jpeg;base64,x', width: 640, height: 480,
             lease: { who: 'agent', for_seconds: 3 } } })
  const lgRow = (lg.props.children || [])[2] || {}
  const lgKids = (lgRow.props || {}).children || []
  ok('  the dock draws the lease as its own row, under the keyboard line',
     lgKids.length === 2
     && /An agent is driving/.test(String((lgKids[0].props || {}).children))
     && String((lgKids[1].props || {}).children) === 'Take it back'
     && (lgKids[1].props || {})['data-lease-release'] === 'rig',
     JSON.stringify(lgKids.map((k) => (k.props || {}).children)))
  ok('  ... and a screen with nobody driving has only the one row',
     (((__fns.GuestScreen({ ctx: lctx, name: 'rig', kick: () => {}, onWarn: () => {},
        frame: { image: 'data:image/jpeg;base64,x', width: 640, height: 480 } })
        .props.children || [])[2]) === null))
  if (lgKids.length === 2) {
    guard('take the keyboard back from an agent', () => lgKids[1].props.onClick())
    await settle()
    ok('  taking it back posts the release route for this machine',
       leaseCalls.length === 1 && leaseCalls[0].p === '/machines/rig/lease/release'
       && leaseCalls[0].o.method === 'POST', JSON.stringify(leaseCalls))
  }

  const mods = (c, a) => ({ ctrlKey: c, altKey: a })
  ok('  Ctrl+Alt on its own hands the keyboard back -- on the way up, and only when both go',
     __fns.keyOwnerStep('no', 'Control', mods(true, false), 'down').owner === 'no'
     && __fns.keyOwnerStep('no', 'Alt', mods(true, true), 'down').owner === 'maybe'
     // Letting go of one while the other is still held is not the gesture: the browser reports
     // ctrlKey true until Ctrl itself comes up, so the release waits for the last of the two.
     && __fns.keyOwnerStep('maybe', 'Alt', mods(true, false), 'up').release === false
     && __fns.keyOwnerStep('maybe', 'Alt', mods(true, true), 'up').release === false
     && __fns.keyOwnerStep('maybe', 'Alt', mods(false, false), 'up').release === true
     && __fns.keyOwnerStep('maybe', 'Control', mods(false, false), 'up').release === true,
     JSON.stringify(__fns.keyOwnerStep('maybe', 'Alt', mods(false, false), 'up')))
  ok('  ... while Ctrl+Alt+F2 still belongs to the guest',
     __fns.keyOwnerStep('maybe', 'F2', mods(true, true), 'down').owner === 'no'
     && __fns.keyOwnerStep('no', 'Alt', mods(true, false), 'up').release === false,
     JSON.stringify(__fns.keyOwnerStep('maybe', 'F2', mods(true, true), 'down')))
  ok('  ... and a single modifier is not a way out either',
     __fns.keyOwnerStep('no', 'Control', mods(true, false), 'down').release === false
     && __fns.keyOwnerStep('no', 'a', mods(false, false), 'down').release === false)

  const bar = (img.props.children || [])[1] || {}
  const barKids = (bar.props || {}).children || []
  const noteNode = barKids[0] || {}, btnNode = barKids[1] || {}
  ok('  the dock draws the line and the button beside it',
     /keyboard/i.test(String((noteNode.props || {}).children))
     && String((btnNode.props || {}).children) === __fns.keyboardButton(true)
     && typeof (btnNode.props || {}).onClick === 'function',
     JSON.stringify(Object.keys(btnNode.props || {})))
  ok('  ... and clicking it hands the keyboard back rather than throwing',
     (() => { try { btnNode.props.onClick(); return true } catch (e) { return false } })(),
     'a button wired to a dead handler is not a way out')
  ok('  ... and the picture hears the key-up the gesture ends on',
     typeof imgNode.props.onKeyUp === 'function'
     && (() => { try {
          imgNode.props.onKeyDown(keyEvent('Control', { target: onPicture, ctrlKey: true, altKey: true }))
          imgNode.props.onKeyDown(keyEvent('Alt', { target: onPicture, ctrlKey: true, altKey: true }))
          imgNode.props.onKeyUp(keyEvent('Alt', { target: onPicture, ctrlKey: true, altKey: false }))
          return true } catch (e) { return false } })(),
     'the whole gesture, run through the handlers the tab really attaches')

  // A drag that ends outside the picture never delivers the mouseup the guest is waiting for;
  // the guest would be left holding the button while the person's mouse is back on the desktop.
  // Mounted fresh so its own window listener is the one being exercised.
  __effects.splice(0)
  const third = __fns.GuestScreen({ ctx: kctx, name: 'rig', kick: () => {},
                                    frame: { image: 'data:image/jpeg;base64,x', width: 640, height: 480 },
                                    onWarn: () => {} })
  __effects.splice(0).forEach((f) => { const c = f(); if (typeof c === 'function') cleanups.push(c) })
  const timg = (third.props.children || [])[0] || {}
  sent.length = 0
  timg.props.onMouseDown({ clientX: 10, clientY: 10 })
  await settle()
  const downs = sent.length
  sent.length = 0
  for (const f of (listens.mouseup || [])) f({})
  await settle()
  ok('  a button held in the guest is let go when the mouse is let go anywhere',
     downs === 1 && sent.length === 1
     && JSON.stringify(sent[0].body)
        === JSON.stringify({ events: [{ kind: 'pointer', buttons: [], x: 10, y: 10 }] }),
     `${downs} down, ${sent.length} up: ${JSON.stringify(sent.map((x) => x.body))}`)

  // One keystroke used to be one request of its own, the moment the DOM event happened, so a
  // fast typist had several in the air at once and the guest saw them in whatever order they
  // came back. Now the second one waits for the first: at most one request is out, what arrived
  // meanwhile goes behind it in order, and nothing is dropped on the way.
  let release = null
  const gated = { rest: (p, o) => {
    sent.push({ p, body: (o && o.body) || null })
    return new Promise((r) => { release = () => r({}) })
  } }
  __effects.splice(0)
  const fourth = __fns.GuestScreen({ ctx: gated, name: 'rig', kick: () => {},
                                     frame: { image: 'data:image/jpeg;base64,x', width: 640, height: 480 },
                                     onWarn: () => {} })
  __effects.splice(0).forEach((f) => { const c = f(); if (typeof c === 'function') cleanups.push(c) })
  const qimg = (fourth.props.children || [])[0] || {}
  sent.length = 0
  qimg.props.onKeyDown(keyEvent('a', { target: onPicture }))
  await settle()
  const outNow = sent.length                     // one request, and it is still out
  qimg.props.onKeyDown(keyEvent('b', { target: onPicture }))
  await settle()
  const whileOut = sent.length                   // the second did NOT start a request of its own
  if (release) release()
  await settle()
  ok('  a keystroke while one is already in flight waits its turn instead of racing it',
     outNow === 1 && whileOut === 1 && sent.length === 2
     && JSON.stringify((sent[1] || {}).body) === JSON.stringify({ events: [{ kind: 'key', key: 'b' }] }),
     `${outNow} out, ${whileOut} while out, ${sent.length} after: ${JSON.stringify(sent)}`)

  /* Reported: "the click-to-drag mouse thing only exists docked in Hermes; in a window of its own
     there is perfect mouse passthrough." The guest's pointer used to move only while a host button
     was held, so hovering did nothing and the next click landed wherever the last drag left it. A
     USB tablet reports an absolute position, so the guest's pointer can simply follow the host's. */
  sent.length = 0
  timg.props.onMouseMove({ clientX: 100, clientY: 100 })
  await settle()
  ok('  hovering the picture moves the guest pointer, with nothing held',
     sent.length === 1 && sent[0].body.events[0].kind === 'pointer'
     && sent[0].body.events[0].buttons.length === 0, JSON.stringify(sent))
  sent.length = 0
  timg.props.onMouseMove({ clientX: 101, clientY: 101 })
  await settle()
  ok('  ... but not once per pixel of travel: motion is throttled',
     sent.length === 0 && __fns.POINTER_MOVE_MS > 0, `${sent.length} sent within the window`)

  /* A right click is a right click. `onDown` used to send ['left'] whatever was pressed, so a
     middle click and a right click both arrived as the left button. */
  sent.length = 0
  timg.props.onMouseDown({ button: 2, clientX: 100, clientY: 100 })
  await settle()
  ok('  a right click reaches the guest as right, not as left',
     sent.length === 1 && sent[0].body.events[0].buttons.join() === 'right', JSON.stringify(sent))
  sent.length = 0
  timg.props.onMouseUp({ button: 2, clientX: 100, clientY: 100 })
  await settle()
  ok('  ... and letting it go leaves nothing held',
     sent.length === 1 && sent[0].body.events[0].buttons.length === 0, JSON.stringify(sent))

  /* A drag that ends outside the picture must not leave the guest holding a button. A leave event
     says nothing about which button was down, so leaving releases all of them. */
  sent.length = 0
  timg.props.onMouseDown({ button: 0, clientX: 10, clientY: 10 })
  await settle()
  sent.length = 0
  timg.props.onMouseLeave({})
  await settle()
  ok('  leaving the picture lets go of every button the guest was told about',
     sent.length === 1 && sent[0].body.events[0].buttons.length === 0, JSON.stringify(sent))
  sent.length = 0
  timg.props.onMouseLeave({})
  await settle()
  ok('  ... and a leave with nothing held says nothing at all', sent.length === 0, `${sent.length} sent`)

  for (const c of cleanups) c()
  ok('  closing the tab stops the listening', (listens.keydown || []).length === 0,
     `${(listens.keydown || []).length} left`)
} catch (e) {
  ok('  the guest keyboard could be exercised', false,
     (e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : String(e)))
} finally {
  globalThis.window = realWindow
}

/* --- a machine's settings, typed rather than spoken ---------------------------
   The rules are the backend's; what the panel owns is the two things a form can get wrong on
   its own: sending a value nobody touched, and keeping a number that is not a number. */
guard('the settings form opens on what the machine already is', () => {
  const m = { name: 'box', cpus: 4, memory_mb: 8192, disk_gb: 40, display: 'panel',
              boot: 'system', ssh: { port: 2222 }, ports: [{ host: 8080, guest: 80 }],
              notes: 'kept for the lab' }
  const f = __fns.machineFields(m)
  return ok('  every setting is there, as text', f.cpus === '4' && f.memory_mb === '8192'
    && f.disk_gb === '40' && f.display === 'panel' && f.boot === 'system'
    && f.ssh_port === '2222' && f.ports === '8080:80' && f.notes === 'kept for the lab', f)
    && ok('  ... and a machine with nothing set is all blanks',
      __fns.machineFields({}).cpus === '' && __fns.machineFields({}).ssh_port === ''
      && __fns.machineFields({}).ports === '')
    && ok('  ... with no machine at all asked for', __fns.machineFields(null).notes === '')
})

guard('a form that sends every field back overwrites what it never showed', () => {
  const m = { name: 'box', cpus: 4, memory_mb: 8192, disk_gb: 40, display: 'panel',
              boot: 'system', ssh: { port: 2222 }, ports: [{ host: 8080, guest: 80 }], notes: '' }
  const untouched = __fns.machineChanges(__fns.machineFields(m), m)
  return ok('  nothing changed is nothing sent', Object.keys(untouched.changes).length === 0,
    JSON.stringify(untouched))
    && ok('  ... and the same ports written differently are still the same ports',
      Object.keys(__fns.machineChanges(Object.assign(__fns.machineFields(m),
        { ports: '8080:80' }), m).changes).length === 0)
    && ok('  ... while a real change is exactly one field',
      JSON.stringify(__fns.machineChanges(Object.assign(__fns.machineFields(m),
        { cpus: '8' }), m).changes) === '{"cpus":8}')
    && ok('  ... and a blank number is left alone rather than read as zero',
      Object.keys(__fns.machineChanges(Object.assign(__fns.machineFields(m),
        { memory_mb: '' }), m).changes).length === 0)
})

guard('a number that is not a number is refused here, in words', () => {
  const m = { name: 'box', cpus: 4, memory_mb: 8192, disk_gb: 40, ports: [], ssh: {} }
  const bad = __fns.machineChanges(Object.assign(__fns.machineFields(m), { cpus: 'lots' }), m)
  const frac = __fns.machineChanges(Object.assign(__fns.machineFields(m), { memory_mb: '2048.5' }), m)
  const shrink = __fns.machineChanges(Object.assign(__fns.machineFields(m), { disk_gb: '20' }), m)
  return ok('  a word where a number goes names the field', !bad.changes && /cores/.test(bad.error), bad)
    && ok('  ... a fraction is not a whole number either', !frac.changes && /memory/.test(frac.error))
    && ok('  ... and a disk is grown, never shrunk', !shrink.changes && /40 GB/.test(shrink.error),
      shrink.error)
    && ok('  ... while growing it is fine',
      __fns.machineChanges(Object.assign(__fns.machineFields(m), { disk_gb: '80' }), m).changes.disk_gb === 80)
})

guard('a port that is not a pair is refused by name, not dropped', () => {
  const good = __fns.portPairs('8080:80\n5432-5432, 9000:9000')
  const bad = __fns.portPairs('8080:80\nnonsense\n70000:80')
  const m = { name: 'box', cpus: 4, ports: [{ host: 8080, guest: 80 }], ssh: {} }
  const refused = __fns.machineChanges(Object.assign(__fns.machineFields(m),
    { ports: '8080:80, nonsense' }), m)
  return ok('  pairs are read from either separator, one per line or comma-separated',
    good.pairs.length === 3 && good.pairs[1].host === 5432 && good.bad.length === 0,
    JSON.stringify(good))
    && ok('  ... and the lines that are not pairs are named', bad.pairs.length === 1
      && bad.bad.join(',') === 'nonsense,70000:80', JSON.stringify(bad))
    && ok('  ... so a typo cannot quietly become a machine with fewer ports',
      !refused.changes && /nonsense/.test(refused.error), refused.error)
    && ok('  ... while clearing the ports is a real change, not an empty string',
      JSON.stringify(__fns.machineChanges(Object.assign(__fns.machineFields(m),
        { ports: '' }), m).changes) === '{"ports":[]}')
    && ok('  ... and removing the ssh port says so, rather than leaving a blank port',
      JSON.stringify(__fns.machineChanges(Object.assign(
        __fns.machineFields({ name: 'b', ssh: { port: 2222 }, ports: [] }),
        { ssh_port: '' }), { name: 'b', ssh: { port: 2222 }, ports: [] }).changes) === '{"ssh":null}')
})

/* ------------------------------------------------- a refused action, tab by tab ---
   One action per region, each answered `{ok: false}` by a backend that talks the way the real
   one does, asserting the *person* is told. The rule these belong to is in t_ui_js.py, which
   proves no mutating call site can skip the answer; what follows is the other half of it, that
   the answer reaches whoever pressed the button. Each case is watched through the same channel
   the panel would use in the app -- the host notification, or the dock's own warning line --
   rather than through a component's internal state, because a message state is not observable
   after a click in a harness without a reconciler. */

const labelNode = (node, label) => {
  const seen = (n) => {
    if (n == null || typeof n === 'string') return String(n == null ? '' : n)
    if (Array.isArray(n)) return n.map(seen).join(' ')
    if (typeof n === 'object') return seen(n.props && n.props.children)
    return ''
  }
  const walkFind = (n, depth) => {
    if (n == null || typeof n !== 'object' || depth > 30) return null
    if (Array.isArray(n)) { for (const c of n) { const hit = walkFind(c, depth + 1); if (hit) return hit } return null }
    if (n.props && typeof n.props.onClick === 'function' && seen(n).trim() === label) return n
    return walkFind(n.props && n.props.children, depth + 1)
  }
  return walkFind(node, 0)
}

const refusal = (route, why) => ({
  host: { notify: null },
  rest: async (p) => (String(p).includes(route) ? { ok: false, error: why } : {}),
})
const settleAll = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve() }

try {
  // Apps: the card's Stop. The card has refused correctly since the engine pass; this keeps it
  // that way, and it is the tab where a refusal is most likely to be seen.
  const appNotes = []
  const actx = refusal('/stop', 'this app is not running')
  actx.host.notify = (n) => appNotes.push(n)
  const card = __fns.AppCard({ ctx: actx, app: { id: 'demo', name: 'Demo', running: true },
                               onChange: async () => {}, row: false })
  const stopBtn = labelNode(card, 'Stop')
  ok('  the apps card offers the stop it was asked for', !!stopBtn,
     stopBtn ? 'found' : 'no button labelled Stop')
  if (stopBtn) {
    stopBtn.props.onClick()
    await settleAll()
    ok('  ... and an apps action the backend refuses is announced, not swallowed',
       appNotes.some((n) => n && n.kind === 'error' && /Demo: stop failed/.test(String(n.message))),
       JSON.stringify(appNotes))
  }

  // The screen's own input route is the third case, and it is checked where it can be decided
  // honestly: `t_ui_js.py` asserts the refusal is handed to the panel by name on that route,
  // and the lease block above drives the keyboard itself. Driving it here as well would mean
  // mounting the tab that owns the read loop, and a second assertion that only re-states the
  // first is a check with no new failure to catch.
  // The header: the engine's own Start. Same shape, different tab of the same panel.
  const engNotes = []
  const ectx = refusal('/engine/start', 'no usable Node on this machine')
  ectx.host.notify = (n) => engNotes.push(n)
  const header = __fns.Header({ ctx: ectx, st: { ready: false, running: false }, err: '', refresh: async () => {} })
  const startBtn = labelNode(header, 'Start')
  if (startBtn) {
    startBtn.props.onClick()
    await settleAll()
  }
  ok('  ... and the engine refusing to start explains itself with the backend\'s own sentence',
     startBtn && engNotes.some((nn) => nn && /no usable Node/.test(String(nn.message))),
     JSON.stringify(engNotes))
} catch (e) {
  ok('  a refused action is announced, tab by tab', false, String(e && e.stack ? e.stack : e))
}

// ---- the working arc -------------------------------------------------------------------------
// Hermes draws a travelling arc around a working session's row; an agent using a card here
// wears the same one. The agent names what it is touching and this turns that name into the
// selector the panel reaches for. Both ways it can be wrong are quiet: an unquoted value
// simply never matches, and a value carrying a quote throws the selector away instead of
// missing, so the failure looks exactly like "the agent is doing nothing".
const ss = __fns.spotSelector
ok('  the key an agent sent becomes the selector that finds the card',
   ss('app:wan') === '[data-athena-spot~="app:wan"]',
   String(ss('app:wan')))
ok('  ... a name with a space is still one key, because the attribute holds a list of them',
   ss('machine:Omarchy VM') === '[data-athena-spot~="machine:Omarchy VM"]',
   String(ss('machine:Omarchy VM')))
ok('  ... and a quote in the name is escaped, so it misses rather than breaking the selector',
   ss('app:o"x') === '[data-athena-spot~="app:o\\"x"]' &&
   ss('app:a\\b') === '[data-athena-spot~="app:a\\\\b"]',
   `${ss('app:o"x')} / ${ss('app:a\\b')}`)
ok('  ... and nothing named is no selector at all, never "the first card"',
   ss('') === '' && ss('   ') === '' && ss(null) === '' && ss(undefined) === '',
   JSON.stringify([ss(''), ss('   '), ss(null)]))

// The card the agent named may not be mounted: only one tab draws each kind. This is the tab
// the panel follows to, and '' means "nowhere the panel draws", which is why it has to be
// distinguished from a tab name rather than defaulting to the first one.
const st = __fns.spotTab
ok('  an app card lives on the apps tab, a store card on store, a machine on machines',
   st('app:wan') === 'apps' && st('store:chatbot-ollama') === 'store' &&
   st('machine:OmarchyVM') === 'machines',
   `${st('app:wan')} / ${st('store:x')} / ${st('machine:y')}`)
ok('  ... and a kind with no card behind it goes nowhere, rather than somewhere wrong',
   st('') === '' && st('tab:apps') === '' && st(null) === '' && st(undefined) === '',
   JSON.stringify([st(''), st('tab:apps'), st(null)]))
ok('  ... and a bare kind is still a kind, because a colon is what a name hangs off',
   st('app') === 'apps' && st('machine') === 'machines',
   `${st('app')} / ${st('machine')}`)
ok('  ... and only the kind decides, so a name with a colon in it cannot pick a tab',
   st('app:we:ird') === 'apps' && st('nope:app') === '',
   `${st('app:we:ird')} / ${st('nope:app')}`)

guard('dispose() does not throw', () => plugin.dispose())

console.log(`\nrest paths requested: ${[...new Set(restCalls)].join(', ') || '(none before await)'}`)
console.log(fail ? `\n${fail} FAILURE(S)` : '\nALL UI CHECKS PASSED')
process.exit(fail ? 1 : 0)

