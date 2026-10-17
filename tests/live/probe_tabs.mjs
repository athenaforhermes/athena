/**
 * The tab bar: which tabs exist, and when the Terminal one does.
 *
 * The merged plugin.js cannot be imported here (plugin-sdk imports, JSX), so
 * `shellTabs` is lifted out of it and called for real. That is the whole point:
 * whether the Terminal tab is standing furniture or appears on demand is a
 * behaviour, not a string, and a syntax check would pass either way.
 *
 * Usage: node probe_tabs.mjs <path to merged plugin.js>
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

// Defaults to the build sitting next to this probe, so a bare
// `node probe_tabs.mjs` tests the real plugin instead of crashing on a missing
// argument — and on any machine, not just the one it was written on.
const file = process.argv[2]
  || join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'desktop', 'plugin.js')
const src = readFileSync(file, 'utf8')

/** The source of one `function name(...) { ... }`, params skipped, braces balanced. */
function fn(name) {
  const i = src.indexOf(`function ${name}(`)
  if (i === -1) throw new Error(`not found in the merged plugin: function ${name}`)
  let paren = 0, j = src.indexOf('(', i)
  for (let k = j; k < src.length; k++) {
    if (src[k] === '(') paren++
    else if (src[k] === ')') { paren--; if (paren === 0) { j = k + 1; break } }
  }
  let depth = 0
  for (let k = j; k < src.length; k++) {
    const c = src[k]
    if (c === '{') depth++
    else if (c === '}') { depth--; if (depth === 0) return src.slice(i, k + 1) }
  }
  throw new Error(`unbalanced: ${name}`)
}

const shellTabs = new Function(`${fn('shellTabs')}; return shellTabs`)()

let passed = 0, failed = 0
const check = (label, ok, detail = '') => {
  if (ok) { passed++; console.log(`  ok   ${label}`) }
  else { failed++; console.log(`  FAIL ${label}   ${detail}`) }
}
const keys = (tabs) => tabs.map(([k]) => k).join(',')

// --- nothing is ever captured ------------------------------------------------
// A passthrough the person cannot leave is a trap, and the mouse is the one thing they cannot
// reason about from a picture. So the pointer is never locked and never captured: the way back
// out is a key (Ctrl+Alt), a click on the dock button, or simply moving the pointer off the
// picture -- which works because it was never taken in the first place.
// Scoped deliberately: the one `setPointerCapture` in the whole plugin is the system tab's
// splitter grip, which is a drag handle and not a screen. What matters is the guest picture.
check('the picture never captures the pointer, so moving off it is always enough',
      !/requestPointerLock|setPointerCapture|pointerLockElement/.test(fn('GuestScreen')),
      'a captured pointer would have to be escaped rather than merely left')
check('... and nothing anywhere takes the pointer away from the person',
      !/requestPointerLock|pointerLockElement/.test(src),
      'pointer lock is the trap a browser cannot be talked out of')
check('... while the keyboard, which IS held while the tab is open, states its way out',
      /function keyboardButton\(/.test(src) && /function keyOwnerStep\(/.test(src)
      && /Ctrl\+Alt on its own gives the keyboard back/.test(src))

// --- with no terminal open ---------------------------------------------------
const closed = shellTabs(null)
check('a closed terminal leaves no terminal tab in the bar', keys(closed) === 'apps,store,files,machines,viewer,runs,settings', keys(closed))
check('nothing is named logs', !keys(closed).includes('logs'))
check('Apps is still the default first tab', closed[0][0] === 'apps')
check('Settings is still last', closed[closed.length - 1][0] === 'settings')

// --- with an app's terminal open ---------------------------------------------
const open = shellTabs('RMBG-2-Studio.git')
check('opening a terminal adds exactly one tab', open.length === closed.length + 1, `${closed.length} -> ${open.length}`)
check('the added tab is the terminal, labelled Terminal', open.some(([k, l]) => k === 'terminal' && l === 'Terminal'))
check('it sits among the app views, not after Runs (history)', open.findIndex(([k]) => k === 'terminal') < open.findIndex(([k]) => k === 'runs'))
check('Settings did not move', open[open.length - 1][0] === 'settings' && open[0][0] === 'apps', keys(open))
check('it is next to App/runs, not after Settings', keys(open) === 'apps,store,files,machines,viewer,terminal,runs,settings', keys(open))

// --- and closing it again ----------------------------------------------------
const reclosed = shellTabs(null)
check('closing it again leaves no trace (no shared array mutated)', keys(reclosed) === keys(closed), keys(reclosed))
const twice = shellTabs('a')
check('one app open is one tab, not one per call', twice.filter(([k]) => k === 'terminal').length === 1)

// --- what the panel and the app card are wired to ----------------------------
check('the panel asks shellTabs for its bar, with the app and the current job',
      src.includes('const tabs = shellTabs(useValue(termApp), useValue(activeJob), useValue(watchMachine))'))

// --- with a job that has no app behind it -------------------------------------
// Updating the whole shelf is output with no single app to point at. It still
// needs the terminal, so the bar has to count a job as well as an open app.
const shelf = shellTabs(null, { app: null, id: 'job-1' })
check('a shelf job puts the terminal in the bar too', shelf.some(([k]) => k === 'terminal'),
      keys(shelf))
check('and it sits in the same place as an app terminal',
      shelf.findIndex(([k]) => k === 'terminal') === open.findIndex(([k]) => k === 'terminal'),
      keys(shelf))
// --- with a machine whose screen is being watched ----------------------------
// A guest is the third thing here with output worth a tab. It is added the same
// way the terminal is -- by name, from a live value -- and it must not shove
// Settings off the end or invent a tab for a machine nobody is looking at.
const watching = shellTabs(null, null, 'OmarchyVM')
check('watching a machine adds one tab, labelled with its name',
      watching.length === closed.length + 1
      && watching.some(([k, l]) => k === 'mscreen' && l === 'OmarchyVM'), keys(watching))
check('it sits among the app views, before Runs', 
      watching.findIndex(([k]) => k === 'mscreen') < watching.findIndex(([k]) => k === 'runs'),
      keys(watching))
check('and it does not displace the job terminal, or Settings',
      keys(shellTabs(null, { app: null, id: 'job-1' }, 'OmarchyVM'))
        === 'apps,store,files,machines,viewer,mscreen,terminal,runs,settings',
      keys(shellTabs(null, { app: null, id: 'job-1' }, 'OmarchyVM')))
check('closing the watch leaves no trace', keys(shellTabs(null, null, null)) === keys(closed),
      keys(shellTabs(null, null, null)))
check('the machine tab body is guarded by the same value it renders',
      src.includes("t === 'mscreen' ? jsx(MachineScreenTab, { ctx }) : null"))
check('the tab is named after the machine, not the word for the tab',
      src.includes("if (machine) tabs.splice(at(), 0, ['mscreen', String(machine)])"))
check('only the machine opens it: Start asks for the tab', src.includes('setWatch(name)'))

// --- typing without a click in front of every letter --------------------------
// The picture was the only listener, so it needed focus, and one click per keystroke is not a
// computer you can install an OS on. The tab listens on the window instead, and the guards are
// what keep that from being rude: a field the person is typing in keeps its keys, and the app's
// own shortcuts are not stolen by a guest.
check('the guest hears keys while the tab is open, not only when its picture has focus',
      src.includes("window.addEventListener('keydown', onWindowKey)"))
check('... and stops listening when the tab closes',
      src.includes("window.removeEventListener('keydown', onWindowKey)"))
check('text being typed into a field of this app stays in that field',
      src.includes("el.closest('input,textarea,select,[contenteditable=\"true\"]')"))
check("the app's own shortcuts are not swallowed by the guest",
      src.includes('if ((e.ctrlKey || e.altKey) && !e.metaKey) return'))
// ...but Super is not one of the app's shortcuts and it is not the panel's either: on a desktop
// like Omarchy it is the keyboard, and dropping it is the reported "the shortcut acted on Hermes
// instead of the guest". A combination that carries Meta goes through, and it carries Super.
check("... while a combination carrying Super belongs to the guest, which is who it is for",
      src.includes("if (e.metaKey) mods.push('Super')")
      && src.includes("if (e.ctrlKey || e.altKey || e.metaKey) {"))
check('one keypress is one keypress: the picture marks what it already sent',
      src.includes('if (e.__athenaSent) return') && src.includes('e.__athenaSent = true'))
// React hands a pane's own handler a synthetic event while the tab's window listener sees the
// native one: a marker on one of them is invisible to the other, and marking only the synthetic
// one sent every key twice ("zack" arrived as "zzaacckk"). So: mark both, and keep a check that
// depends on neither object.
check('... on the native event too, not only the wrapper React handed it',
      src.includes('if (e.nativeEvent) e.nativeEvent.__athenaSent = true'))
check('... and on the target, which both listeners can name',
      src.includes("el.classList.contains('ah-screenimg')"))
check('and the panel does not claim a click is needed for every key',
      !src.includes('whenever this picture has focus'))

// --- the app's type-to-focus reflex, and the scope that stops it -----------------
// A printable key landing on ordinary chrome focuses the chat composer and types there: that is
// "it automatically resets into our session for me to type over here", and it is why one click
// bought exactly one key. Hermes blocks it when focus is inside a `[data-terminal]` scope, which
// is what its own remote-screen pane marks itself with, so this pane marks itself the same way.
check('the picture claims the keyboard scope, so the chat cannot take a letter back',
      src.includes("'data-terminal': ''") && src.includes("'data-remote-screen': ''"))
check('... on the host as well as the picture, so a click anywhere in it counts',
      /ah-screendock', 'data-terminal'/.test(src))
check('opening the tab puts focus in that scope, so no click is needed to begin',
      src.includes('if (!a || a === document.body || a === document.documentElement) el.focus()'))
// Guarded, not eager: the focus call is behind a check that nothing is being typed into and
// behind a null check on the ref, so mounting this tab cannot pull the caret out of a draft.
check('... politely: the focus call cannot reach an element that is not there, or a live draft', (() => {
  const i = src.indexOf('const a = document.activeElement')
  if (i < 0) return false
  const before = src.slice(Math.max(0, i - 200), i)
  const after = src.slice(i, i + 200)
  return before.includes('if (!el) return') && after.includes('el.focus()')
})())
check('a screen moved into a window of its own is not read by the panel at all',
      src.includes("String(display_ || 'panel') === 'panel'"))
check('and the tab says where that screen went instead of drawing a stale one',
      src.includes('drawn there and not here') && src.includes('bring it back '))

/* Two buttons that promised different things and, for a QEMU machine, did the same
   destructive one: "Forget it (keeps the distro)" deleted the folder the distro was in.
   Forgetting now moves the machine aside; destroying is the only thing that deletes. */
check('the panel reads what was put aside, so a forgotten machine is not invisible',
      src.includes("ctx.rest('/machines/forgotten'"))
check('... and offers it back, through the one route that moves the folder whole',
      src.includes('/machines/forgotten/${encodeURIComponent(name)}/restore'))
check('... with each button saying what it does before it does it', (() => {
  const f = src.slice(src.indexOf("if (what === 'forget')"), src.indexOf("if (what === 'destroy')"))
  const d = src.slice(src.indexOf("if (what === 'destroy')"), src.indexOf("if (what === 'destroy')") + 400)
  return f.includes('Nothing is deleted') && d.includes('This is the one that deletes')
})())
check('... and a machine whose disk holds a system stops calling itself an installer',
      src.includes("m.installed) bits.push('its own system on disk')"))

/* The lag was not the backend: measured here, a keystroke leaves in ~20 ms and the guest has
   drawn it in ~139 ms, while the panel looked again every 1.2 s and then every 2.5 s. The
   wiring that fixes it has to be present in the built file, not just in the source. */
check('input asks for the picture back, instead of waiting for the reader\'s own next look',
      src.includes('const kick = useCallback(() => {') && src.includes('lastInput.current = Date.now()'))
check('... and both halves of sending do it: before it leaves, and once it has landed', (() => {
  const i = src.indexOf('const post = useCallback(async (events) => {')
  if (i < 0) return false
  /* To the end of the function, not to a fixed width: a 900-character window meant this check
     failed whenever anything was added above the second call, which is a check measuring the
     size of the file rather than the wiring. */
  const endsAt = src.indexOf('useCallback', i + 40)
  const body = src.slice(i, endsAt > 0 ? endsAt : i + 1200)
  return body.includes('kick()') && body.split('kick()').length >= 3
})())
// A keystroke used to be a request of its own, the instant it happened, so a fast typist had
// several in the air and the guest saw them in reply order. One at a time, the rest queued.
check('... and one batch is out at a time, with the rest queued behind it in order',
      src.includes('if (inFlight.current) { waiting.current.push(...events); return inFlight.current }')
      && src.includes('await post(waiting.current.splice(0, waiting.current.length))'))
check('... and hovering moves the guest pointer instead of requiring a drag',
      src.includes('if (!buttons.current.length && p.x === spot.current.x && p.y === spot.current.y) return')
      && src.includes('send([{ kind: \'pointer\', buttons: buttons.current, x: p.x, y: p.y }])'))
check('... and the picture is named for what it is, because the live one is not a PNG',
      src.includes('src: f.image') && !src.includes('f.png')
      && src.includes('const hasPic = !!f.image'))
check('... and the wheel is a native listener, because React attaches wheel passively',
      src.includes("el.addEventListener('wheel', onWheelHere, { passive: false })"))
check('... and a right click is not the app context menu',
      src.includes("const BUTTON_NAMES = { 0: 'left', 1: 'middle', 2: 'right' }"))
check('... and a look already due is never pushed further away, only brought in',
      src.includes('const wait = kickDelay(deadlineRef.current, now)')
      && src.includes('if (prevDeadline > now && prevDeadline <= now + INPUT_ECHO_MS) return null'))
check('... and the reader uses that, so the cadence follows the person rather than a habit',
      src.includes('frameDelay(snap, still.current.n, Date.now() - lastInput.current)'))
check('... through a timer a kick can pre-empt, rather than one it has to wait out',
      src.includes('if (timerRef.current) clearTimeout(timerRef.current)')
      && src.includes('timerRef.current = setTimeout('))
check('a screen that is not being drawn is not read, and its reader is stood down',
      src.includes('liveRef.current = false') && src.includes('tickRef.current = null'))

/* A ref is a name in one scope. The lag wiring was written in the tab *above* the picture and the
   names were left behind in the picture, so the built plugin threw "liveRef is not defined" the
   moment the tab mounted and the whole workspace failed to render — while every check above this
   line passed, because each of them only asked whether a string was present. Presence is not
   resolution. This asks where each name is declared, relative to where it is used. */
function unresolvedRefs(text) {
  const stack = [], scopes = []
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (c === '{') stack.push(i)
    else if (c === '}') { const s = stack.pop(); if (s !== undefined) scopes.push({ start: s, end: i }) }
  }
  const owner = (at) => {
    let best = -1, width = Infinity
    for (const s of scopes) {
      if (s.start < at && at < s.end && (s.end - s.start) < width) {
        width = s.end - s.start; best = s.start
      }
    }
    return best
  }
  const decls = []
  const dre = /const\s+([A-Za-z_$][\w$]*)\s*=\s*useRef\(/g
  let m
  while ((m = dre.exec(text))) {
    const nameAt = m.index + m[0].indexOf(m[1])
    decls.push({ name: m[1], at: nameAt, owner: owner(nameAt) })
  }
  // A name that is also a plain local, a parameter or a prop is not a ref name: `busy` is a
  // `useRef` in one function and `const busy = o.busy || ''` in another, and the second is
  // correct. Only a name whose every declaration is a useRef has to resolve to one.
  const plain = new Set()
  const pre = /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/g
  while ((m = pre.exec(text))) {
    const nameAt = m.index + m[0].indexOf(m[1])
    if (!/^\s*useRef\(/.test(text.slice(m.index + m[0].length))) plain.add(m[1])
  }
  const missing = []
  for (const name of [...new Set(decls.map((d) => d.name))]) {
    if (plain.has(name)) continue
    // A ref is used as a ref: `N.current`, read or written. A bare `N` may be a prop or a local.
    const ure = new RegExp(`\\b${name}\\s*\\.\\s*current\\b`, 'g')
    let u
    while ((u = ure.exec(text))) {
      const at = u.index
      const line = text.slice(0, at).split('\n').length
      const seen = decls.some((d) => {
        if (d.name !== name || d.at >= at) return false
        if (d.owner === -1) return true                                 // declared at module level
        return scopes.some((s) => s.start === d.owner && s.start < at && at < s.end)
      })
      if (!seen) missing.push(`${name}.current at line ${line}, declared in no enclosing scope`)
    }
  }
  return missing
}

const dangling = unresolvedRefs(src)
check('every ref is used where it is declared — a ref named in one function and used in another',
      dangling.length === 0, dangling.slice(0, 3).join('; '))
check('... and that check can fail: the shape that crashed the workspace is reported',
      unresolvedRefs(`function A() {\n const liveRef = useRef(false)\n return 1\n}\nfunction B() {\n if (liveRef.current) return 2\n}`)
        .some((s) => s.includes('liveRef')), 'positive control')
check('... and a name that is a plain local somewhere is not dragged into the check',
      unresolvedRefs(`function A() {\n const busy = useRef(false)\n return 1\n}\nfunction B(o) {\n const busy = o.busy || ''\n return busy\n}`).length === 0,
      'busy is both a ref and a local in this file')
check('... and it does not cry wolf over a ref used inside its own function',
      unresolvedRefs(`function A() {\n const box = useRef(null)\n return () => box.current\n}`).length === 0)
check('... or over one used in a nested callback of its own function',
      unresolvedRefs(`function A() {\n const tickRef = useRef(null)\n useEffect(() => { tickRef.current = 1 }, [])\n}`).length === 0)
// The prop and the ref it carries are the seam this bug came through: the picture calls the
// kick, the tab owns it, and neither half works if only one of them says so.
check('the picture takes the kick as a prop rather than owning a ref of its own',
      src.includes('function GuestScreen({ ctx, name, frame, onWarn, kick })'))
check('... the tab declares it where the read loop lives, and hands it down',
      src.includes('const kick = useCallback(() => {')
      && src.includes('jsx(GuestScreen, { ctx, name, frame, onWarn: setNote, kick })'))
check('... and the read loop the kick hurries is in that same function',
      (() => {
        const i = src.indexOf('function MachineScreenTab(')
        const j = src.indexOf('function ', i + 1)
        const body = src.slice(i, j)
        return body.includes('const kick = useCallback')
          && body.includes('tickRef.current = tick')
          && body.includes('frameDelay(snap, still.current.n, Date.now() - lastInput.current)')
      })())

check('the terminal body is guarded by the same value it renders',
      src.includes("t === 'terminal' ? jsx(TerminalTab, { ctx }) : null"))
check('only the app can open it', src.includes('const openTerminal = () => { termApp.set(app.id); tab.set(\'terminal\') }'))
check('the fetch is the terminal route, and reads output',
      src.includes('/terminal`, { timeoutMs: 30000 })') && src.includes("typeof r.output === 'string'"))
check('the header shows where the app is serving', src.includes('setLive(r.url || \'\')'))

// --- jobs, tabs, and sentences that arrive too early ----------------------------
// Nothing here is about looks. Each one is a sentence the panel used to say, or a tab it used
// to draw, about work that had not happened yet.
check('a job asked for but not yet named is not polled like one that exists',
      /const mine = job && job\.id && \(!app \|\| job\.app === app\) \? job : null/.test(src)
      && /const starting = job && !job\.id \? String\(job\.label \|\| 'Working'\) : ''/.test(src))
check('... and the tab says what it is waiting for instead of drawing nothing',
      /\$\{starting\} — starting…/.test(src)
      && /if \(!app && !mine && !starting\) \{/.test(src))
check('the terminal tab leaves when the job it was showing does',
      /termApp\.set\(null\); activeJob\.set\(null\); tab\.set\('apps'\)/.test(src)
      && /if \(!installing\) \{ termApp\.set\(null\); tab\.set\('apps'\) \}/.test(src))
check('updating everything does not end on the tab that only exists while it runs',
      /const updateAll[\s\S]{0,2600}?finally \{\s*setUpdBusy\(false\)[\s\S]{0,600}?activeJob\.set\(null\)\s*tab\.set\('apps'\)/
        .test(src))
check('an install from a url says installed only once the build that follows has finished',
      /if \(r && r\.ok && r\.job\) \{/.test(src)
      && /Installed and built\. See the Apps tab\./.test(src)
      && /await awaitJob\(ctx, r\.job, \{ onTick: \(j\) => \{/.test(src))
check('a button that deletes copies, or spends the space it saved, asks first — with the figure',
      /runSure\('\/disk\/free', 'Free unshared copies'\)/.test(src)
      && /runSure\('\/disk\/separate', 'Restore independent copies'\)/.test(src)
      && /goes back to the drive/.test(src) && /is used again, in exchange for each app/.test(src))

// --- what a machine is, changed from the panel ---------------------------------
// The rules belong to the backend; what the panel owns is what it types and what it says back.
// A form that invents its own bounds, or posts back a value nobody touched, is a form that
// quietly disagrees with the same machine changed by an agent.
check('a machine can be changed from its own card',
      /onClick: \(\) => \{ onMenu\(''\); onSettings\(m\.name\) \}/.test(src)
      && /children: 'Settings…'/.test(src))
check('... by a form that asks the backend and reports its sentence',
      /method: 'PATCH'/.test(fn('MachineSettings'))
      && /if \(r && r\.ok === false\)/.test(fn('MachineSettings'))
      && /setNote\(\(r && r\.note\)/.test(fn('MachineSettings')))
check('... sending only what is different, never the whole form back',
      /if \(same\(key\)\) continue/.test(fn('machineChanges'))
      && /const changes = \{\}/.test(fn('machineChanges')))
check('... and a number a box cannot hold is refused in words, not sent',
      /has to be a whole number/.test(fn('machineChanges'))
      && /a disk can be grown and not shrunk/.test(fn('machineChanges')))
check('one reader for a port pair, so the two forms cannot disagree about one',
      /function parsePorts\(text\) \{\s*return portPairs\(text\)\.pairs/.test(src)
      && (src.match(/\[:>=\-\]/g) || []).length === 1)

// --- the store's search box ---------------------------------------------------
// A search box is not a keyboard macro. Every character used to fire its own request at a store
// that answers in seconds, with a 90-second timeout apiece, and the replies could arrive in any
// order -- so the list under the box was whichever answer happened to land last. What has to be
// true is: one request per pause, and only the newest question's answer is drawn.
const storeSrc = fn('StoreTab')
check('the store search waits for a pause instead of asking once per keystroke',
      /setTimeout\(\(\) => \{\s*if \(mine === searchSeq\.current\) fetchPage\(/.test(storeSrc),
      'onQuery must schedule the fetch, not perform it')
check('... and the wait is a named number, not a literal in the middle of it',
      /const SEARCH_SETTLE_MS = \d+/.test(src) && /searchTimer\.current = setTimeout/.test(storeSrc))
check('... and a superseded answer is dropped rather than drawn',
      /if \(mine !== searchSeq\.current\) return/.test(storeSrc))
check('... with a tag click or a sort change cancelling anything still being typed',
      /const supersede = \(\) => \{/.test(storeSrc)
      && /supersede\(\)\s*fetchPage\(q, 0, false, value, sort\)/.test(storeSrc))

// --- the guest picture's geometry ---------------------------------------------
// `object-fit: contain` means the drawn picture and the element's box are not the same
// rectangle. Clicks are measured against the picture, and the box is sized to match it, so a
// guest that is a different shape from the pane does not shift every click by the margin.
const gpSrc = fn('guestPoint')
check('a click is measured against the picture that was drawn, not the box around it',
      /padX/.test(gpSrc) && /padY/.test(gpSrc) && /Math\.min\(rw \/ w, rh \/ h\)/.test(gpSrc))
check('... and the box itself is sized to the picture rather than to the column',
      /\.ah-screenimg \{ display: block; width: auto; max-width: 100%;/.test(src))

console.log(failed ? `\n${passed} passed, ${failed} failed` : `\n${passed}/${passed} checks passed`)
process.exit(failed ? 1 : 0)
