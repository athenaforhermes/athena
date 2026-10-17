/**
 * Athena — desktop half of the unified `athena` package.
 *
 * The local AI app store for Hermes. Athena has its own store front end: it
 * treats the upstream catalogue strictly as a data source, then draws every
 * card, list and button itself. Nothing upstream is embedded, framed, or
 * re-skinned, and no upstream branding appears anywhere in this UI.
 *
 * The Python half (dashboard/plugin_api.py) owns the engine and the catalogue.
 * This half only renders and calls.
 *
 * Plain ESM, loaded uncompiled — jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */
import {
  Badge, Button, Codicon, ContextMenu, ContextMenuContent, ContextMenuItem,
  ContextMenuSeparator, ContextMenuTrigger, GlyphSpinner, ListRow, Popover,
  PopoverContent, PopoverTrigger, SearchField, STATUSBAR_AREAS, StatusDot,
  Tip, ToggleRow, atom, host, usePluginI18n, useQuery, useValue
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'athena'
const POLL_MS = 5000
// While the engine is coming up, look again in a second. A cold boot is six
// seconds of a port that accepts and says nothing, and a five-second clock on
// top of that meant "it came up" could be seen five seconds late — the second
// half of the wait people felt at launch.
const STARTING_POLL_MS = 1000
// The status-bar chip is a smaller thing than the panel and asks less often once
// the engine is settled.
const CHIP_POLL_MS = 8000
/* How long the store's search box waits after the last keystroke before asking. Long enough
   that a word arrives complete, short enough that it still reads as instant. */
const SEARCH_SETTLE_MS = 350

// Last known values, deliberately outside React. The window is not a route:
// host.openWorkspace tears the whole pane tree down on close and rebuilds it on
// open, so every remount began from empty state and painted "Loading apps…" —
// and then "The engine is stopped." — over data that had never changed. The
// module outlives the pane, so seeding from here means a reopen shows the real
// list on the first frame and revalidates quietly behind it.
let lastStatus = null
let lastApps = null
const PAGE = 48

const tab = atom('apps')
// Which app's terminal is open. Deliberately not a standing tab: the tab exists
// while this is set, and the app's own Terminal button is the only thing that
// sets it. A log is history and history is the Runs tab — this is now.
const termApp = atom(null)
// A long action — a fix, an install — runs on the terminal, so the tab needs to
// know which job belongs to the app it is showing. Kept here rather than on the
// card: the card can be scrolled away or the tab switched, and the job still has
// output worth reading.
const activeJob = atom(null)
// Which machine's screen is open. Like the terminal tab, it exists only while someone is
// watching: pressing Start, or a machine's own button, is what sets it. A guest's screen is
// something you look at, so it is a tab and not a thumbnail -- a 640x480 installer squeezed
// into a card is a picture of a computer rather than a computer you can use.
const watchMachine = atom(null)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// How long until the next look at /status, as a plain function of the last
// answer: `ready` is a settled state and can wait the long clock; anything else
// is a boot in progress and gets a short one. Exported for the UI test, because
// the difference between one second and five is the thing being fixed.
function pollDelay(st) {
  return (st && st.ready) ? POLL_MS : STARTING_POLL_MS
}
// Which app the viewer pane is showing, and the address it was launched on.
// Kept in an atom so the Apps tab can open it from a plain button handler.
const viewer = atom(null)

// The mini monitor is a second home for the same reading, not a second
// monitor. `sysBar` is whether it sits in the status bar beside the music
// widget. An atom rather than state so the toggle in Settings takes effect at
// once instead of at the next reload. Seeded from storage in register().
const sysBar = atom(true)

/* Persist and apply together: the atom must move even if storage is
   unavailable, so a failure there can never leave the switch lying about
   what is on screen. */
function setSysBar(ctx, v) {
  sysBar.set(!!v)
  try { ctx.storage.set('sysBar', !!v) } catch { /* noop */ }
}

/* ---- The plugin window ----------------------------------------------------
   The status-bar chip is this window's open/close switch, so the window has to
   be something that can actually leave. `host.openWorkspace` is the SDK's own
   door for a plugin window: it registers the pane, fronts it, and returns a
   closer that does the full teardown (dispose + removeTreePane). Calling it
   again with the same id fronts the existing tab instead of stacking a second
   copy, which is what makes the chip a toggle rather than an opener.
   `winOpen` tracks whether the window is registered, and the pane's own Close
   routes back through here via `onClose`, so the chip cannot lie about it.
   On a desktop without the API, register() falls back to the old
   always-registered panes and the chip can only front them. */
const winOpen = atom(true)
let athenaCtx = null
let winClose = null
let viewerClose = null
let systemClose = null
// The ported system panel lives in the merged half of this file, so this half
// cannot name it. The merge registers a factory here as the plugin loads; until
// it does, the deep view shows the ranked consumers on their own.
let systemFullFactory = null
function registerSystemPanel(fn) { systemFullFactory = fn }

const WINDOW_ID = 'athena-window'
const VIEWER_WINDOW_ID = 'athena-viewer'
const WINDOW_PANE = 'plugin-workspace:' + WINDOW_ID

function hasWorkspaceApi() {
  return !!(host && typeof host.openWorkspace === 'function')
}

function openAthenaWindow() {
  if (!athenaCtx || !hasWorkspaceApi()) return
  try {
    winClose = host.openWorkspace(WINDOW_ID, {
      title: 'Athena',
      minWidth: '22rem',
      // A right-hand track beside the workspace, which is where the pane has
      // always sat. `center` would stack it over the chat.
      dock: { pane: 'workspace', pos: 'right' },
      onClose: () => { winClose = null; winOpen.set(false) },
      render: () => jsx(Panel, { ctx: athenaCtx }),
    })
    winOpen.set(true)
  } catch {
    winClose = null
    winOpen.set(false)
  }
}

/* Front the window, opening it if it is closed: every "go here" inside the
   panel wants this, and a reveal against a closed window is a no-op. */
function revealAthena() {
  if (!hasWorkspaceApi()) {
    try { if (host && typeof host.revealPane === 'function') host.revealPane(ID + ':athena-pane') } catch { /* noop */ }
    return
  }
  if (!winOpen.get() || typeof winClose !== 'function') { openAthenaWindow(); return }
  try { if (typeof host.revealPane === 'function') host.revealPane(WINDOW_PANE) } catch { /* noop */ }
}

function closeViewerWindow() {
  try { if (typeof viewerClose === 'function') viewerClose() } catch { /* noop */ }
  viewerClose = null
}

function closeAthenaWindow() {
  // The viewer belongs to the same app; leaving it behind would strand an
  // app's interface with no way back to the panel that explains it. The deep
  // monitor is the same story — it was opened from inside this window.
  closeViewerWindow()
  try { if (typeof systemClose === 'function') systemClose() } catch { /* noop */ }
  systemClose = null
  try { if (typeof winClose === 'function') winClose() } catch { /* noop */ }
  winClose = null
  winOpen.set(false)
}

function toggleAthenaWindow() {
  if (!hasWorkspaceApi()) { revealAthena(); return }
  if (winOpen.get() && typeof winClose === 'function') closeAthenaWindow()
  else openAthenaWindow()
}

/* The viewer is its own window rather than a tab inside the manager: an app's
   own interface is the thing you look at while it runs, and it wants the room,
   not a 620px column shared with the catalogue. Re-opening fronts the existing
   tab instead of stacking a second copy. */
function openViewerWindow() {
  if (!athenaCtx || !hasWorkspaceApi()) return
  try {
    viewerClose = host.openWorkspace(VIEWER_WINDOW_ID, {
      title: 'App',
      minWidth: '22rem',
      dock: { pane: 'workspace', pos: 'right' },
      onClose: () => { viewerClose = null },
      render: () => jsx(Viewer, { ctx: athenaCtx }),
    })
  } catch { viewerClose = null }
}

const CSS = `
.ah-root { display:flex; flex-direction:column; height:100%; font-size:12px; overflow:hidden; }
.ah-head { display:flex; align-items:center; gap:8px; padding:8px 10px; border-bottom:1px solid var(--ui-stroke-tertiary); flex:0 0 auto; }
.ah-dot { width:8px; height:8px; border-radius:50%; background:var(--ui-text-quaternary); flex:0 0 auto; }
.ah-chip.on { background:var(--muted, var(--ui-bg-secondary)); color:var(--foreground, var(--ui-text-primary)); }
.ah-dot.on { background:var(--ui-green); box-shadow:0 0 6px color-mix(in srgb, var(--ui-green) 53%, transparent); }
.ah-dot.err { background:var(--ui-red); }
.ah-title { font-weight:600; }
.ah-sub { color:var(--ui-text-tertiary); font-size:11px; }
.ah-pace-ok { color:var(--ui-green); }
.ah-pace-warn { color:#eab308; }
.ah-grow { flex:1 1 auto; }
.ah-tabs { display:flex; gap:2px; padding:6px 8px 0; border-bottom:1px solid var(--ui-stroke-tertiary); flex:0 0 auto; }
.ah-tabb { background:none; border:none; color:var(--ui-text-tertiary); padding:6px 10px; cursor:pointer; font-size:12px; border-radius:calc(var(--radius-scalar) * .375rem) 6px 0 0; }
.ah-tabb:hover { color:var(--ui-text-primary); background:var(--chrome-action-hover); }
.ah-tabb.active { color:var(--ui-text-primary); border-bottom:2px solid var(--ui-text-primary); font-weight:600; }
.ah-body { flex:1 1 auto; overflow:auto; padding:10px; }
.ah-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:8px; }
/* Rows instead of cards — the same card laid out sideways, chosen in Settings.
   Everything inside is identical: same chip, same primary verb, same menu. */
.ah-grid.ah-rows { display:flex; flex-direction:column; gap:6px; }
.ah-lrow { flex-direction:row; align-items:center; gap:10px; }
.ah-lrow .ah-cartop { flex:0 0 auto; min-width:150px; }
.ah-lrow p { flex:1 1 200px; min-width:0; margin:0; }
.ah-lrow .ah-row { flex:0 0 auto; }
.ah-lrow .ah-foot { margin-top:0; padding-top:0; border-top:0; }
.ah-clip { display:-webkit-box; -webkit-line-clamp:1; -webkit-box-orient:vertical; overflow:hidden; }
.ah-foot { display:flex; align-items:center; gap:6px; margin-top:2px; padding-top:7px;
  border-top:1px solid var(--ui-stroke-tertiary); flex-wrap:wrap; }
.ah-state { font-size:10px; line-height:1.7; border-radius:20px; padding:0 7px;
  border:1px solid var(--ui-stroke-tertiary); color:var(--ui-text-tertiary); white-space:nowrap; }
.ah-state.run { color:#3fb950; border-color:rgba(63,185,80,.45); background:rgba(63,185,80,.10); }
.ah-state.need { color:#d29922; border-color:rgba(210,153,34,.45); background:rgba(210,153,34,.10); }
.ah-kebab { font:inherit; font-size:14px; line-height:1; padding:2px 8px; border-radius:calc(var(--radius-scalar) * .375rem);
  border:1px solid var(--ui-stroke-tertiary); background:transparent; color:var(--ui-text-tertiary); cursor:pointer; }
.ah-kebab:hover, .ah-kebab.ah-open { color:var(--ui-text-primary); border-color:var(--accent); }
.ah-menu { display:flex; flex-direction:column; gap:1px; border:1px solid var(--ui-stroke-tertiary);
  border-radius:var(--radius-md); background:var(--ui-bg-editor); padding:4px; box-shadow:0 6px 18px rgba(0,0,0,.35); }
.ah-mi { font:inherit; font-size:11px; text-align:left; padding:4px 8px; border-radius:5px;
  border:0; background:transparent; color:var(--ui-text-primary); cursor:pointer; }
.ah-mi:hover:not(:disabled) { background:var(--accent); color:var(--accent-foreground, #fff); }
.ah-mi:disabled { opacity:.45; cursor:default; }
.ah-mi.ah-danger { color:#f85149; }
.ah-mi.ah-misep { border-top:1px solid var(--ui-stroke-tertiary); margin-top:3px; padding-top:5px; border-radius:0 0 5px 5px; }
.ah-card { border:1px solid var(--ui-stroke-tertiary); border-radius:var(--radius-md); padding:10px; display:flex; flex-direction:column; gap:7px; background:var(--ui-bg-editor); position:relative; }
.ah-cartop { display:flex; gap:8px; align-items:flex-start; }
.ah-ico { width:28px; height:28px; border-radius:calc(var(--radius-scalar) * .375rem); object-fit:cover; flex:0 0 auto; background:var(--ui-bg-secondary); }
.ah-icoph { width:28px; height:28px; border-radius:calc(var(--radius-scalar) * .375rem); flex:0 0 auto; background:var(--ui-bg-secondary); display:flex; align-items:center; justify-content:center; color:var(--ui-text-tertiary); }
.ah-card h4 { margin:0; font-size:12px; line-height:1.3; display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
.ah-card p { margin:0; color:var(--ui-text-tertiary); font-size:11px; line-height:1.45; max-height:46px; overflow:hidden; }
.ah-repo { color:var(--ui-text-tertiary); font-size:10px; font-family:var(--font-mono); word-break:break-all; }
.ah-row { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.ah-badge { font-size:10px; padding:1px 6px; border-radius:999px; border:1px solid var(--ui-stroke-tertiary); color:var(--ui-text-tertiary); white-space:nowrap; }
.ah-badge.run { color:var(--ui-green); border-color:color-mix(in srgb, var(--ui-green) 40%, transparent); }
/* An app that wants something from you is not an app that is broken: an amber
   edge, never the red of a failure. */
.ah-badge.need { color:var(--ui-yellow); border-color:#f59e0b66; }
.ah-badge.own { color:var(--accent, var(--ui-accent)); border-color:color-mix(in srgb, var(--accent, var(--ui-accent)) 45%, transparent); }
.ah-empty { color:var(--ui-text-tertiary); text-align:center; padding:28px 12px; line-height:1.6; }
.ah-updbar { display:flex; gap:8px; align-items:center; margin:0 0 8px; padding:7px 10px; border:1px solid #eab308; border-radius:var(--radius-md); background:var(--ui-bg-editor); }
.ah-updtext { font-size:12px; color:var(--foreground, var(--ui-text-primary)); }
.ah-updnote { font-size:11px; color:var(--ui-text-tertiary); }
.ah-search { display:flex; gap:8px; align-items:center; margin:0 0 8px; }
/* The filter row: same chip shape as the app's own chrome, so the list reads as
   part of the window rather than a panel bolted onto it. */
.ah-filtrow { display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin:0 0 8px; }
.ah-filtrow .ah-chip { cursor:pointer; font-size:11px; }
.ah-searchin { flex:1 1 auto; min-width:0; background:var(--card, var(--ui-bg-editor)); color:var(--foreground, var(--ui-text-primary)); border:1px solid var(--border, var(--ui-stroke-primary)); border-radius:calc(var(--radius-scalar) * .375rem); padding:5px 8px; font-size:12px; }
.ah-searchin:focus { outline:none; border-color:var(--accent, var(--ui-accent)); }
/* The first field anyone meets on the machines tab, and it has to look like one: a label, a
   dashed slot that stops being dashed once it has something in it, and a hint that reads as an
   instruction rather than as a value that is already there. */
.ah-namefield { display:flex; align-items:center; gap:6px; flex:1 1 220px; min-width:0; }
.ah-flabel { flex:0 0 auto; font-size:11px; letter-spacing:.02em; text-transform:uppercase; color:var(--muted-foreground, var(--ui-text-secondary)); }
.ah-namein { flex:1 1 auto; min-width:0; background:var(--card, var(--ui-bg-editor)); color:var(--foreground, var(--ui-text-primary)); border:1px dashed var(--border, var(--ui-stroke-primary)); border-radius:calc(var(--radius-scalar) * .375rem); padding:5px 8px; font-size:12px; }
.ah-namein.filled { border-style:solid; }
.ah-namein::placeholder { color:var(--muted-foreground, var(--ui-text-secondary)); opacity:.75; }
.ah-namein:focus { outline:none; border-style:solid; border-color:var(--accent, var(--ui-accent)); }
.ah-why { flex:0 0 auto; font-size:11px; color:var(--muted-foreground, var(--ui-text-secondary)); }
.ah-runsec { border:1px solid var(--ui-stroke-tertiary); border-radius:var(--radius-md); padding:8px 10px; margin-bottom:8px; background:var(--ui-bg-editor); }
.ah-runhead { font-size:11px; color:var(--ui-text-tertiary); margin-bottom:6px; }
.ah-runrow { display:flex; gap:8px; align-items:center; padding:3px 0; }
.ah-rundot { width:7px; height:7px; border-radius:999px; background:var(--ui-green); flex:0 0 auto; }
.ah-runname { font-size:12px; flex:0 0 auto; }
.ah-runurl { font-size:10px; color:var(--ui-text-tertiary); font-family:var(--font-mono); flex:1 1 auto; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ah-runbtns { display:flex; gap:6px; flex:0 0 auto; }
/* The guest's screen inside the panel. Scaled to the card's width and never taller than
   the space a card has, so a 1280-wide guest does not push the panel sideways; the picture
   keeps its own aspect, so a click lands where it looks like it lands. */
.ah-screendock { display: flex; flex-direction: column; gap: 6px; margin: 6px 0; }
/* A machine's screen in its own tab: the picture is the point, so it gets the room. The
   body is a flex box with a minimum of nothing, which is what lets a picture taller than
   the pane shrink instead of pushing the notes off the bottom. */
.ah-mscreen { display: flex; flex-direction: column; gap: 8px; height: 100%; min-height: 0; }
.ah-mtitle { font-weight: 600; }
.ah-mbtns { margin-left: auto; display: flex; gap: 6px; }
.ah-mbody { flex: 1 1 auto; min-height: 0; display: flex; align-items: center;
  justify-content: center; }
.ah-mbody .ah-screendock { width: 100%; height: 100%; margin: 0; }
.ah-mbody .ah-screenimg { max-height: calc(100vh - 260px); }
.ah-screenimg { display: block; width: auto; max-width: 100%; height: auto; max-height: 70vh; object-fit: contain;
  /* The picture's own frame, not a surface: a loaded screenshot covers it entirely, so what
     shows through is only ever the letterbox while one is arriving -- and a person on a light
     theme should not see a black rectangle where the picture is going to be. */
  background: var(--card, #0b0b0f); border: 1px solid var(--ui-stroke-tertiary); border-radius: 6px; cursor: crosshair;
  outline: none; }
.ah-screenimg:focus { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.ah-screenblank { padding: 18px 12px; text-align: center; color: var(--ui-text-tertiary);
  background: var(--card, #0b0b0f); border: 1px solid var(--ui-stroke-tertiary); border-radius: 6px; font-size: 12px; }
.ah-pre { font-family:var(--font-mono); font-size:11px; white-space:pre-wrap; word-break:break-all; background:var(--ui-bg-editor); border:1px solid var(--ui-stroke-tertiary); border-radius:calc(var(--radius-scalar) * .375rem); padding:8px; max-height:46vh; overflow:auto; }
.ah-kv { display:flex; justify-content:space-between; gap:10px; padding:3px 0; border-bottom:1px dashed var(--ui-stroke-tertiary); }
.ah-kv span:first-child { color:var(--ui-text-tertiary); }
.ah-warn { color:var(--ui-yellow); }
.ah-ok { color:var(--ui-green); }
.ah-notice { border:1px solid var(--ui-stroke-tertiary); border-radius:calc(var(--radius-scalar) * .375rem); padding:8px 10px; margin-bottom:8px; background:var(--ui-bg-editor); }
/* A folder in the image list: one line, clickable, with what is inside it after the
   name. Reads as a heading when shut and as a heading that opened when open. */
.ah-group { display:block; width:100%; text-align:left; font:inherit; font-size:11px; font-weight:600;
  color:var(--ui-text-tertiary); background:none; border:0; border-radius:calc(var(--radius-scalar) * .375rem); padding:4px 6px;
  margin-top:6px; cursor:pointer; }
.ah-group:hover { color:var(--ui-text-primary); background:var(--ui-bg-editor); }
.ah-group.ah-open { color:var(--ui-text-primary); }
.ah-ingroup { padding-left:10px; }
/* Something that has to happen before a machine can run. Marked out rather than
   colored — it is a to-do, not an error. */
.ah-need { border-left:2px solid var(--ui-stroke-tertiary); padding-left:8px; margin:6px 0; }
.ah-needtitle { font-size:12px; font-weight:600; }
.ah-code { display:inline-block; font-family:var(--font-mono); font-size:11px;
  background:var(--ui-bg-editor); border:1px solid var(--ui-stroke-tertiary); border-radius:calc(var(--radius-scalar) * .375rem); padding:3px 6px;
  word-break:break-all; }
/* A guest's console: monospace, top-aligned, and short enough to sit inside a card. */
.ah-log { max-height:32vh; }
.ah-term { font-family:var(--font-mono); font-size:10px; white-space:pre-wrap; word-break:break-all; background:var(--ui-bg-editor); border:1px solid var(--ui-stroke-tertiary); border-radius:calc(var(--radius-scalar) * .375rem); padding:6px 8px; max-height:150px; overflow:auto; margin:0; color:var(--ui-text-tertiary); }
.ah-detail { border:1px solid var(--ui-stroke-tertiary); border-radius:calc(var(--radius-scalar) * .375rem); padding:8px; display:flex; flex-direction:column; gap:6px; }
.ah-dl { display:grid; grid-template-columns:auto 1fr; gap:2px 10px; margin:0; font-size:11px; }
.ah-dl dt { color:var(--ui-text-tertiary); white-space:nowrap; }
.ah-dl dd { margin:0; word-break:break-all; }
.ah-pick { cursor:pointer; }
.ah-pick input { flex:0 0 auto; }
.ah-pick span:nth-child(2) { flex:1 1 auto; text-align:left; word-break:break-all; }

/* progress — a real bar rather than a bare "2/3" */
.ah-prog { display:flex; flex-direction:column; gap:4px; }
.ah-prog + .ah-prog { margin-top:8px; }
.ah-progtop { display:flex; justify-content:space-between; gap:8px; align-items:baseline; font-size:11px; }
.ah-proglab { color:var(--ui-text-primary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ah-progval { color:var(--ui-text-tertiary); font-variant-numeric:tabular-nums; flex:0 0 auto; }
.ah-bar { height:6px; border-radius:999px; background:color-mix(in srgb, var(--ui-text-tertiary) 22%, transparent); overflow:hidden; }
.ah-barfill { height:100%; border-radius:999px; background:var(--ui-text-primary); opacity:.72; transition:width .3s ease; }
.ah-barfill.done { opacity:.45; }
.ah-barfill.failed { background:var(--ui-red); opacity:.85; }
.ah-barfill.indet { width:35%; animation:ah-slide 1.15s ease-in-out infinite; }
@keyframes ah-slide { 0%{margin-left:-35%} 100%{margin-left:100%} }
.ah-steps { display:flex; flex-direction:column; gap:2px; margin-top:2px; }
.ah-step { display:flex; gap:7px; align-items:baseline; font-size:11px; color:var(--ui-text-tertiary); }
.ah-stepname { flex:1 1 auto; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ah-stepstate { flex:0 0 auto; font-size:10px; }
.ah-stepstate.done { color:var(--ui-green); }
.ah-stepstate.failed { color:var(--ui-red); }
/* The button a step on a check offers. The rows are one short line each, so the
   button has to fit inside one rather than reflow the list it belongs to. */
.ah-stepbtn { flex:0 0 auto; padding:1px 7px; font-size:10px; }
.ah-note { font-size:11px; color:var(--ui-text-tertiary); margin-top:3px; }
/* Who holds the keyboard, and the one click that changes it. The note takes the room; the
   button takes the little it needs, because the button is the way out for somebody whose
   keyboard is not answering this window any more. */
.ah-notebar { display:flex; align-items:center; gap:8px; margin-top:3px; }
.ah-notebar .ah-note { flex:1; margin-top:0; }
.ah-kbswitch { flex:none; font-size:11px; padding:2px 8px; cursor:pointer; border-radius:calc(var(--radius-scalar) * .25rem);
  border:1px solid var(--ui-stroke-tertiary); background:transparent; color:var(--ui-text-tertiary); }
.ah-kbswitch:hover { color:var(--ui-text-primary); border-color:var(--accent); }
/* Why an app stopped, above the checks: the failing program's own sentence is the
   evidence, so it is shown as text rather than summarised, and the one action that
   answers it sits with it. A left edge rather than a filled panel, because this is
   often a note about history and should not shout like a fire. */
.ah-fail { border:1px solid var(--ui-stroke-tertiary); border-left:2px solid var(--ui-red); border-radius:3px;
  padding:5px 7px; margin:2px 0 7px; background:rgba(239,68,68,.06); }
.ah-failtitle { font-size:11px; font-weight:600; }
.ah-failwhy { font-size:11px; color:var(--ui-text-tertiary); margin-top:2px; }
.ah-failev { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:10px;
  color:var(--ui-text-tertiary); margin-top:3px; white-space:pre-wrap; word-break:break-word;
  max-height:76px; overflow:auto; }
.ah-failalso { font-size:10px; color:var(--ui-text-tertiary); margin-top:3px; }
.ah-failrow { display:flex; align-items:center; gap:6px; margin-top:4px; }
/* What needs you, at the top of the System window. One short line per thing, each
   with the step that answers it, because this window is what someone opens when
   something is not working — and the answer to "is anything wrong here" should not
   be a list they have to read carefully. */
.ah-need { border:1px solid var(--ui-stroke-tertiary); border-radius:3px; padding:5px 7px; margin:4px 0; }
.ah-need.warn { border-left:2px solid var(--ui-red); background:rgba(239,68,68,.06); }
.ah-need.info { border-left:2px solid var(--ui-yellow); }
.ah-needtitle { font-size:11px; font-weight:600; }
.ah-needwho { font-size:10px; color:var(--ui-text-tertiary);
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.ah-needwhy { font-size:11px; color:var(--ui-text-tertiary); margin-top:2px;
  overflow-wrap:anywhere; }
.ah-needrow { display:flex; align-items:center; gap:6px; margin-top:4px; }
.ah-needhead { font-size:11px; }
.ah-needquiet { font-size:11px; color:var(--ui-text-tertiary); }

/* viewer — the app's own web UI, plus the terminal behind it */
.ah-vroot { display:flex; flex-direction:column; height:100%; overflow:hidden; }
.ah-vhead { display:flex; align-items:center; gap:6px; padding:6px 8px; border-bottom:1px solid var(--ui-stroke-tertiary); flex:0 0 auto; }
.ah-vname { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ah-vurl { color:var(--ui-text-tertiary); font-size:10px; font-family:var(--font-mono); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
/* The video preview's own backdrop: white is what a player expects behind video, but it has to
   give way to the app's surface so an empty or unloaded frame is not a white slab on a dark app. */
.ah-vframe { flex:1 1 auto; border:none; width:100%; background:var(--card, #fff); }
.ah-vterm { flex:1 1 auto; overflow:auto; margin:0; padding:10px; font-family:var(--font-mono); font-size:11px; line-height:1.5; white-space:pre-wrap; word-break:break-word; color:var(--ui-text-primary); }
.ah-vwait { flex:1 1 auto; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px; color:var(--ui-text-tertiary); padding:24px; text-align:center; }
.ah-secthead { font-weight:600; font-size:12px; margin:16px 0 6px; }
.ah-secthead:first-child { margin-top:0; }
.ah-sec { border:1px solid var(--ui-stroke-tertiary); border-radius:calc(var(--radius-scalar) * .375rem); margin:0 0 8px; overflow:hidden; }
.ah-sec:first-child { margin-top:0; }
.ah-sec-open > .ah-secb { border-bottom:1px solid var(--ui-stroke-tertiary); }
.ah-secb { display:flex; align-items:center; width:100%; gap:6px; padding:7px 9px;
  background:transparent; border:0; color:inherit; font:inherit; text-align:left; cursor:pointer; }
.ah-secb:hover { background:var(--accent, var(--chrome-action-hover)); }
.ah-secchev { flex:0 0 auto; width:10px; opacity:.6; font-size:10px; }
.ah-sectitle { font-weight:600; font-size:12px; }
.ah-secsum { margin-left:auto; font-size:11px; opacity:.6; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.ah-secbody { padding:2px 9px 9px; }
.ah-sect { margin-bottom:16px; }
.ah-sect > h3 { margin:0 0 2px; font-size:12px; font-weight:600; }
.ah-sect > .ah-sub { margin-bottom:8px; }
.ah-card.tight { gap:5px; }
/* The choices an app offers when it declares no default action. Its own menu is
   the only honest source, so they are listed as they are named, with the
   submenu path kept visible — "Start 48GB RAM + 12GB VRAM" means nothing
   without the model and mode above it. */
.ah-choices { display:flex; flex-direction:column; gap:3px; max-height:210px; overflow:auto;
  border:1px solid var(--ui-stroke-tertiary); border-radius:calc(var(--radius-scalar) * .375rem); padding:4px; }
.ah-choice { display:flex; flex-direction:column; align-items:flex-start; gap:1px; text-align:left;
  background:transparent; border:0; border-radius:calc(var(--radius-scalar) * .25rem); padding:5px 7px; cursor:pointer; width:100%;
  color:var(--ui-text-primary); font:inherit; font-size:11px; line-height:1.35; }
.ah-choice:hover { background:var(--chrome-action-hover); }
.ah-choice:disabled { opacity:.5; cursor:default; }
.ah-choicepath { color:var(--ui-text-tertiary); font-size:10px; }
.ah-choicetext { font-weight:500; }
.ah-live { color:var(--ui-green); }
.ah-btn { background:none; border:1px solid var(--ui-stroke-tertiary); color:var(--ui-text-primary); padding:4px 9px; border-radius:calc(var(--radius-scalar) * .375rem); cursor:pointer; font-size:11px; }
.ah-btn:hover:not(:disabled) { background:var(--chrome-action-hover); }
.ah-btn:disabled { opacity:.5; cursor:default; }
/* ----------------------------------------------- sending a file to an app */
/* The ported browser wants the whole pane, so it keeps it and the send bar is a
   sibling under it: the explorer takes the room that is left. */
.ah-files { display:flex; flex-direction:column; height:100%; min-height:0; }
.ah-files > .hermes-fb { flex:1 1 auto; min-height:0; }
.ah-then { border-top:1px dashed var(--ui-stroke-tertiary); padding-top:8px; display:flex; flex-direction:column; gap:6px; }
.ah-then .ah-send-head { font-weight:600; }
.ah-send { flex:0 0 auto; border-top:1px solid var(--ui-stroke-tertiary); background:var(--card, var(--ui-bg-editor)); padding:8px 10px; display:flex; flex-direction:column; gap:6px; max-height:42%; overflow:auto; }
.ah-send.idle { color:var(--ui-text-tertiary); font-size:11px; }
.ah-send-head { display:flex; align-items:center; gap:8px; font-weight:600; }
.ah-send-what { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ah-send-grow { flex:1 1 auto; }
.ah-send-rows { display:flex; flex-direction:column; gap:4px; }
.ah-send-row { display:flex; align-items:center; gap:8px; border:1px solid var(--ui-stroke-tertiary); border-radius:calc(var(--radius-scalar) * .375rem); padding:6px 8px; }
.ah-send-info { flex:1 1 auto; min-width:0; }
.ah-send-app { font-weight:600; }
.ah-send-line { color:var(--ui-text-tertiary); font-size:11px; }
.ah-send-note { color:var(--ui-text-tertiary); font-size:10px; }
.ah-send-notes { display:flex; flex-direction:column; gap:2px; color:var(--ui-text-tertiary); font-size:10px; }
.ah-send-job { display:flex; align-items:center; gap:8px; color:var(--ui-text-tertiary); font-size:11px; }
.ah-send-file { font-family:var(--font-mono, ui-monospace,Consolas,monospace); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ah-send-hint { margin-left:auto; opacity:.7; }
.ah-btn.primary { background:var(--ui-text-primary); color:var(--ui-bg-chrome); border-color:var(--ui-text-primary); }
/* ------------------------------------------------------- mini monitor */
/* One reading, two homes: the status-bar chip beside the music widget and the
   compact card the System tab collapses to. Numbers are tabular so a row does
   not twitch sideways as values change width. */
.ah-mm-bar { display:flex; align-items:center; height:100%; }
.ah-mm-chip { display:inline-flex; align-items:center; gap:4px; font-size:11px; font-variant-numeric:tabular-nums; white-space:nowrap; }
.ah-mm-chip > span { color:var(--muted-foreground, var(--ui-text-tertiary)); }
.ah-mm-chip > b { font-weight:600; color:var(--foreground, var(--ui-text-primary)); }
.ah-mm-sep { color:var(--border, var(--ui-stroke-primary)); padding:0 1px; font-size:11px; }
.ah-mm-panel { width:312px; max-width:calc(100vw - 24px); }
.ah-mm-head { display:flex; align-items:center; gap:6px; padding:0 2px 8px; font-size:12px; font-weight:600; }
.ah-mm-off { font-size:11px; color:var(--muted-foreground, var(--ui-text-tertiary)); padding:2px; }
.ah-mm-meter { padding:4px 2px 5px; }
.ah-mm-top { display:flex; align-items:baseline; justify-content:space-between; gap:8px; font-size:11px; color:var(--muted-foreground, var(--ui-text-tertiary)); }
.ah-mm-top > b { font-weight:600; color:var(--foreground, var(--ui-text-primary)); font-variant-numeric:tabular-nums; }
.ah-mm-track { height:4px; margin-top:4px; border-radius:2px; background:var(--accent, var(--chrome-action-hover)); overflow:hidden; }
.ah-mm-fill { height:100%; border-radius:2px; background:var(--accent, var(--ui-accent)); transition:width .35s ease; }
.ah-mm-fill[data-warn=true] { background:#eab308; }
.ah-mm-fill[data-hot=true] { background:var(--ui-red); }
.ah-mm-row { display:flex; align-items:baseline; gap:8px; font-size:11px; color:var(--muted-foreground, var(--ui-text-tertiary)); padding:2px; }
.ah-mm-row > b { margin-left:auto; font-weight:600; color:var(--foreground, var(--ui-text-primary)); font-variant-numeric:tabular-nums; }
.ah-mm-group { font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted-foreground, var(--ui-text-quaternary)); padding:9px 2px 3px; }
.ah-mm-foot { display:flex; align-items:center; gap:8px; margin-top:8px; padding-top:8px; border-top:1px solid var(--border, var(--ui-stroke-tertiary)); }
.ah-systab { display:flex; flex-direction:column; height:100%; min-height:0; }
.ah-systab-bar { display:flex; align-items:center; gap:8px; flex:0 0 auto; padding:6px 2px 8px; }
.ah-systab-full { display:flex; flex-direction:column; gap:10px; min-height:0; }
.ah-sysmin { flex:1 1 auto; min-height:0; overflow:auto; display:flex; flex-direction:column; align-items:center; padding-top:4px; }
.ah-sysmin > * { width:100%; max-width:340px; }
.ah-mono { font-family:var(--font-mono, ui-monospace,Consolas,monospace); font-size:11px; color:var(--muted-foreground, var(--ui-text-secondary)); }
/* A number the user sets. Real tokens, not the preview-widget aliases: an
   undefined var() here is not an error, it is a control that renders unstyled. */
.ah-num { width:74px; background:var(--card, var(--ui-bg-editor)); color:var(--foreground, var(--ui-text-primary)); border:1px solid var(--border, var(--ui-stroke-tertiary)); border-radius:calc(var(--radius-scalar, 6px) * .375rem); padding:3px 6px; font:inherit; font-size:11px; text-align:right; }
.ah-num:focus { outline:none; border-color:var(--accent, var(--ui-accent)); }

/* -- runs: the history of what the agent ran here ------------------------- */
.ah-runs { display:flex; flex-direction:column; gap:6px; padding:8px 10px; overflow:auto; flex:1 1 auto; min-height:0; }
.ah-repeats { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.ah-repeat { padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; }
.ah-repeatwhen { display: flex; align-items: baseline; gap: 4px; font-size: 12px; }
.ah-run { padding:0; overflow:hidden; }
.ah-runrow { display:flex; align-items:center; gap:8px; padding:8px 10px; cursor:pointer; }
.ah-runrow:hover { background:var(--accent, var(--chrome-action-hover)); }
.ah-runapp { font-weight:600; color:var(--foreground, var(--ui-text-primary)); }
.ah-runintent { color:var(--muted-foreground, var(--ui-text-secondary)); }
.ah-rundetail { padding:4px 10px 10px 10px; border-top:1px solid var(--border, var(--ui-stroke-primary)); display:flex; flex-direction:column; gap:6px; }
.ah-runfiles { display:flex; flex-direction:column; gap:6px; }
.ah-runfile { display:flex; align-items:center; gap:8px; padding:6px; border:1px solid var(--border, var(--ui-stroke-tertiary)); border-radius:calc(var(--radius-scalar) * .375rem); }
.ah-runfile.mine { border-color:var(--accent, var(--ui-accent)); }
.ah-thumb { width:56px; height:56px; object-fit:cover; border-radius:calc(var(--radius-scalar) * .25rem); background:var(--muted, var(--ui-bg-secondary)); }
.ah-runfilemeta { display:flex; flex-direction:column; min-width:0; }
.ah-runfilename { color:var(--foreground, var(--ui-text-primary)); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ah-running { color:#eab308; border-color:#eab308; }
.ah-card.ah-run:has(.ah-running) { border-color:#eab308; }

/* --- the working arc: Hermes' own travelling border, borrowed ------------------
   Hermes Desktop draws an arc around a sidebar row while that session is working
   (arc-border, in apps/desktop/src/styles.css). Athena borrows it so an agent
   driving this panel reads the same way at a glance: the card being acted on wears
   the arc while the tool call is in flight, and a few passes after it lands.

   Two invariants come over with it, because both are load-bearing:
     * travel is a transform on a ::before layer, never background-position — the
       background-position version cost ~3,600 main-thread style recalcs per minute
       per arc;
     * one cycle moves the layer by 50% of its 240% size, which is exactly one period
       of the gradient, so the loop is seamless. The stops are fractions of that.

   No backticks anywhere in this block: the whole stylesheet is a JS template literal,
   so one of them in a comment ends the string and the plugin stops parsing. */
@keyframes athena-arc { 0% { transform: translate(0, 0); } 100% { transform: translate(-50%, -50%); } }
.athena-arc {
  --arc-c1: var(--ui-text-primary);
  /* A visible tail: with only a bright stop surviving each pass the ring reads as
     moving dots instead of an arc, so the trail stop is tied to the bright one. */
  --arc-c2: color-mix(in srgb, var(--arc-c1) 45%, transparent);
  --arc-angle: 160deg;
  --arc-width: 2px;
  /* Flush, not outset. Like the sidebar row it copies, this ring sits on a host
     inside a scrolling column, so an outset run loses its left and right edges to
     the clip. */
  --arc-standoff: 0px;
  --arc-radius: 8px;
  --arc-duration: 2.23s;
  pointer-events: none;
  position: absolute;
  z-index: 5;
  overflow: hidden;
  border-radius: calc(var(--arc-radius) + var(--arc-standoff));
  inset: calc(var(--arc-standoff) * -1);
  padding: var(--arc-width);
  /* The ring itself: a filled box XORed with its own content box leaves only the
     border-width frame, which the ::before gradient travels through. rgb(0 0 0)
     is a stencil here, not a surface — any opaque colour masks identically. */
  mask: linear-gradient(rgb(0 0 0) 0 0) content-box, linear-gradient(rgb(0 0 0) 0 0);
  -webkit-mask-composite: xor;
  mask-composite: exclude;
}
.athena-arc::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 240%;
  height: 240%;
  background: repeating-linear-gradient(
    var(--arc-angle),
    transparent 0%,
    transparent 18.75%,
    var(--arc-c1) 25%,
    var(--arc-c2) 31.25%,
    transparent 43.75%,
    transparent 50%
  );
  will-change: transform;
  animation: athena-arc var(--arc-duration) linear infinite;
}
@media (prefers-reduced-motion: reduce) {
  .athena-arc::before { animation: none; }
  /* Motion is what carries the eye around the ring, so with motion off the arc has
     to say its piece standing still: keep the bright stop and tint it. */
  .athena-arc { --arc-c1: var(--accent); }
}

`

/* ---------------------------------------------------------------- helpers */

// ---- the working arc --------------------------------------------------------
// Hermes Desktop draws a travelling arc around a sidebar row while that session is
// working. This is the same arc, worn by whatever an agent is using in this panel:
// the agent broadcasts `spot` naming what it touched, the card carries that name in
// a space-separated attribute, and the ring is a span appended to the card.

// Pure, so the harness can assert it: the key an agent named, as a selector. Quoted
// because every key carries a colon, escaped because a name with a quote in it has to
// be a miss rather than a selector that throws.
function spotSelector(key) {
  const k = String(key == null ? '' : key).trim()
  if (!k) return ''
  return '[data-athena-spot~="' + k.replace(/["\\]/g, '\\$&') + '"]'
}

// One arc per card, so a second spotlight replaces the first instead of stacking
// rings, and the timer is tracked so a card can be spotted again without leaking one.
const spotRings = new Map()
function spotArc(key, ms) {
  const sel = spotSelector(key)
  if (!sel) return false
  let el = null
  try { el = document.querySelector(sel) } catch { return false }
  if (!el) return false
  const had = spotRings.get(el)
  if (had) { clearTimeout(had.timer); had.node.remove() }
  const node = document.createElement('span')
  node.className = 'athena-arc'
  node.setAttribute('aria-hidden', 'true')
  el.appendChild(node)
  const hold = Math.max(600, Math.min(20000, Number(ms) || 3000))
  const timer = setTimeout(() => { node.remove(); spotRings.delete(el) }, hold)
  spotRings.set(el, { node, timer })
  return true
}

// Which tab draws which kind of card. The agent names a card; only one tab has it mounted.
const SPOT_TABS = { app: 'apps', store: 'store', machine: 'machines' }

// Pure: the tab a key belongs on, or '' for a key that belongs nowhere the panel draws.
function spotTab(key) {
  const kind = String(key == null ? '' : key).split(':')[0]
  return SPOT_TABS[kind] || ''
}

// A card only exists while its tab is on screen, so naming one you cannot see means the agent
// is working somewhere else in the panel. Follow it there — the same thing `screen.wanted`
// already does for a machine's console — and draw the ring once the tab has painted. Bounded,
// because a card that never appears has to give up quietly instead of leaving a timer running.
function spotFollow(key, ms, setTab) {
  if (spotArc(key, ms)) return 'here'
  const want = spotTab(key)
  if (!want || !setTab) return 'nowhere'
  setTab(want)
  let left = 12
  const tick = () => {
    if (spotArc(key, ms)) return
    if (--left > 0) setTimeout(tick, 50)
  }
  setTimeout(tick, 50)
  return 'following'
}

function notify(ctx, kind, message) {
  try {
    if (ctx.host && ctx.host.notify) ctx.host.notify({ kind, message })
  } catch { /* notifications are best effort */ }
}

/* A backend that answers 200 with {ok: false} has NOT succeeded, and the engine
   routes are the ones that talk that way: a start with no usable Node, a spawn
   that dies, a port that never answers. Reading only the status code made every
   one of those a button that flashed "Starting…", went back to "stopped" and
   explained nothing — the same silence the whole release pass was about, in the
   one place a stranger meets it first. Returns the sentence to show, or '' when
   there is nothing to say. */
function actionFailure(label, res) {
  if (!res || typeof res !== 'object' || res.ok !== false) return ''
  const why = res.error || res.detail
  return why ? `${label}: ${why}` : `${label}: failed`
}

// How long a run has to take before finishing is worth interrupting someone for.
// A run the agent just asked for and watched finish in four seconds does not need
// a notification; the one that took a quarter of an hour while you were reading
// email does. Below this the record still lands in Runs — this only decides
// whether it also makes a noise.
const RUN_QUIET_SECS = 20

function runSecs(s) {
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${Math.round(s - m * 60)}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

/* What a *running* job should say about itself, and what a finished one took.
   The two numbers that answer the only question a person watching an install has
   — how long has this been going, and how long since it said anything — are on
   the job record as ages, so nothing here is estimated. There is deliberately no
   "expected 3 minutes": Athena keeps no history of how long an install took, and
   a made-up estimate is worse than no estimate. Instead, when a silence is long
   enough to be worth explaining, this names what that kind of job is usually
   doing while it is quiet — which is the honest version of the same reassurance. */
const JOB_QUIET_HINTS = {
  install: 'a clone walks the whole repository before it copies anything',
  update: 'a fetch can be silent while it negotiates',
  build: 'a build can compile for a while without printing',
  pull: 'a download can be silent between chunks',
  copy: 'a large copy prints nothing between files',
  convert: 'a conversion writes its file at the end',
  render: 'a render can be quiet for minutes at a time',
}

/* Where an app has been, and the one button that takes it back.
   Read out of the app's own repository: a reflog entry exists for every commit
   this folder has actually stood on, so what is offered here is never a guess at
   a "previous version" — it is a commit this app was running before, and the route
   refuses anything else.

   Pure, and handed its callback rather than reaching for `ctx`, so the driver can
   draw every state it has: a folder that is not a repository, an app that has only
   ever been on one commit, a working tree that would lose changes, and a put-back
   already going. */
function putBackPanel(data, opts) {
  const o = opts || {}
  const d = data || {}
  const busy = o.busy || ''
  if (!d.ok) {
    return jsx('div', { className: 'ah-sub ah-warn', children:
      "Could not read this app's history — " + (d.error || 'no reason given') })
  }
  if (!d.is_repo) {
    return jsx('div', { className: 'ah-sub', children:
      d.reason || "this app keeps no history to go back to" })
  }
  const commits = d.commits || []
  // A folder that has only ever stood on one commit has nothing to offer, so it
  // gets the sentence that says so rather than a one-row list where the only thing
  // in it is where the app already is.
  if (!commits.length || d.can_revert === false) {
    return jsx('div', { className: 'ah-sub', children:
      d.reason || "there is nothing to put back to" })
  }
  const rows = commits.map((c) => jsx('div', { className: 'ah-runrow', key: c.sha + '|' + (c.when || ''), children: [
    jsx('span', { className: 'ah-mono', children: c.short || String(c.sha || '').slice(0, 12) }),
    jsx('span', { className: 'ah-sub', style: { flex: '0 0 auto' }, children: c.when || '' }),
    jsx('span', { className: 'ah-grow ah-sub', style: { minWidth: 0 }, children: c.what || '' }),
    c.current
      ? jsx('span', { className: 'ah-sub', children: 'where it is now' })
      : jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
          onClick: () => { if (o.onPutBack) o.onPutBack(c) },
          children: busy === c.sha ? 'Putting back…' : 'Put back' }),
  ] }))
  // What the reset would give up, counted before anyone is asked to agree to it.
  const cost = []
  if (d.dirty) cost.push(d.dirty + ' file' + (d.dirty === 1 ? '' : 's') + ' with changes that are not in a commit')
  if (d.ahead) cost.push(d.ahead + ' commit' + (d.ahead === 1 ? '' : 's') + ' made in this folder')
  return jsxs('div', { className: 'ah-sub', children: [
    jsx('div', { children: "Where this app has been — its own repository's record, newest first." }),
    ...rows,
    cost.length
      ? jsx('div', { className: 'ah-warn', children:
          'Putting it back would give up ' + cost.join(' and ') + '.' })
      : null,
    jsx('div', { children: "Going back leaves the app behind its remote again — its own Update brings it forward." }),
  ] })
}

function jobPace(snap) {
  const j = snap || {}
  const known = (v) => typeof v === 'number' && v >= 0
  if (!j.state) return { text: '', tone: '', title: '' }
  const el = known(j.elapsed) ? j.elapsed : null
  if (j.state !== 'running') {
    const took = el === null ? '' : ' ' + runSecs(el)
    return {
      text: j.state === 'done' ? ('took' + took) : ('gave up after' + (took || ' a while')),
      tone: j.state === 'done' ? 'ok' : 'warn',
      title: el === null ? 'the record carries no usable start stamp'
        : `${j.kind || 'job'} ran for ${runSecs(el)}`,
    }
  }
  // Any recent sign of life counts as a sign of life: a bar that moved half a
  // second ago means the job is working even if it is printing nothing.
  const signs = [j.since_output, j.since_move].filter(known)
  const quiet = signs.length ? Math.min.apply(null, signs) : null
  const bar = known(j.since_move) && known(j.since_output)
    ? j.since_move < j.since_output : !known(j.since_output)
  const ago = (n) => runSecs(n) + ' ago'
  const hint = JOB_QUIET_HINTS[String(j.kind || '').toLowerCase()] || ''
  const title = (j.kind ? `${j.kind}; ` : '')
    + (el === null ? 'started an unknown while ago' : `running ${runSecs(el)}`)
    + (quiet === null ? '' : `, last ${bar ? 'progress' : 'output'} ${ago(quiet)}`)
    + '. Athena keeps no record of how long this one usually takes, so it will '
    + 'not pretend to know.'
  if (quiet === null) return { text: 'running', tone: '', title }
  if (el !== null && el < 3) return { text: 'just started', tone: '', title }
  if (quiet < 15) {
    return { text: bar ? `working — progress ${ago(quiet)}` : `working — output ${ago(quiet)}`,
      tone: 'ok', title }
  }
  if (quiet < 90) return { text: `quiet for ${runSecs(quiet)}`, tone: '', title }
  return { text: `no output for ${runSecs(quiet)}` + (hint ? ` — ${hint}` : ''),
    tone: 'warn', title }
}

/* What a finished run should say. A run is not a job: it has an artifact, and
   the artifact is the interesting half of the sentence — so the notice names the
   file rather than announcing "finished". Pure and exported so the wording can
   be asserted, because the two ways to get this wrong are both text: claiming a
   file exists when the run produced none, and inventing a duration it does not
   have. An unknown duration is simply left out; a run that answered and produced
   nothing says exactly that, because "finished" would be a lie by omission. */
function runDoneWords(p) {
  const j = p || {}
  const app = j.app || 'App'
  const files = Array.isArray(j.files) ? j.files.filter((f) => f && f.path) : []
  const mine = files.find((f) => f.path === j.likely_output) || files[0]
  const known = typeof j.seconds === 'number' && j.seconds >= 0
  const secs = known ? ` in ${runSecs(j.seconds)}` : ''
  const quiet = known && j.seconds < RUN_QUIET_SECS
  if (j.state === 'failed') {
    const why = String(j.error || '').trim().split('\n')[0].slice(0, 90)
    return { kind: 'error', quiet, run: j,
      text: `${app} failed${secs}${why ? ' — ' + why : ''}` }
  }
  if (!mine) {
    return { kind: 'info', quiet, run: j, text:
      `${app} finished${secs} but no file came out — the record is in Runs` }
  }
  const extra = files.length > 1 ? ` (+${files.length - 1} more)` : ''
  return { kind: 'info', quiet, run: j,
    text: `${app} finished${secs} — ${mine.name || 'a file'}${extra}` }
}

// Jobs the panel is already watching. The backend broadcasts every completion,
// and a job the panel is polling reports itself with a message that says more
// than "finished" — so the broadcast only speaks for the jobs nobody is
// watching (the tab was switched away from, or the window closed).
const watchedJobs = new Set()

// Long operations — copying or deleting an app that can run to hundreds of
// gigabytes — run as jobs on the backend so the panel never sits on a request
// that looks like a freeze. This polls one to completion, reporting progress.
// What to say about a job that is not there any more.
//
// Jobs live in the engine's process: an id is a thread and a log tail, and a
// restart takes both. The panel polls one every second and a half, so a restart
// mid-install is seen on the very next tick — as a 404, which used to be reported
// as the request failing. Someone watching an install did not have a network
// problem; the engine they were talking to was replaced underneath them. The
// backend says so in the body when it can (`gone_with_restart` plus its own
// wording), and when all this side has is the error text it still refuses to
// call a restart a network fault.
const JOB_GONE = "This job is not in this run of Hermes — jobs end when Hermes restarts. Whatever it printed is in the app's own log; start it again to watch it through."

function jobGoneWords(err, fallback) {
  const carried = [err, err && err.body, err && err.response]
    .filter((one) => one && typeof one === 'object')
  for (const one of carried) {
    if (one.gone_with_restart && one.note) return String(one.note)
  }
  const said = String((err && (err.message || err.error)) || err || '')
  if (/no such job/i.test(said)) return JOB_GONE
  return fallback
}

// What the shelf says when a shelf-wide update has finished.
//
// One sentence, and it is the only account of what happened that the person sees
// without opening a terminal — so it names the count, and when something failed it
// says where the reason is rather than leaving a number that looks like a total.
// Written out here rather than inline in the handler because the handler cannot be
// run under the suite (its state is not observable), and a sentence nobody can
// assert is a sentence that rots.
function updateAllWords(done, failed) {
  if (failed) return `${done} updated, ${failed} did not — the Terminal says why`
  return done ? `${done} updated` : 'nothing needed updating'
}

async function awaitJob(ctx, jobId, { onTick, timeoutMs = 3600000, everyMs = 1500 } = {}) {
  const deadline = Date.now() + timeoutMs
  // Every exit point goes through settle(): while this poll is alive the caller
  // reports the outcome itself, so the completion event stays quiet for this id.
  watchedJobs.add(jobId)
  const settle = (r) => { watchedJobs.delete(jobId); return r }
  for (;;) {
    let job
    try {
      job = await ctx.rest(`/jobs/${encodeURIComponent(jobId)}`, { timeoutMs: 20000 })
    } catch (e) {
      return settle({ ok: false, error: jobGoneWords(e, String(e)) })
    }
    if (onTick) onTick(job)
    if (job && (job.state === 'done' || job.state === 'failed')) {
      return settle(job.state === 'done'
        ? { ok: true, result: job.result, job }
        : { ok: false, error: job.error || 'job failed', job })
    }
    if (Date.now() > deadline) return settle({ ok: false, error: 'timed out', job })
    await new Promise((r) => setTimeout(r, everyMs))
  }
}

function useEngine(ctx) {
  const [st, setSt] = useState(() => lastStatus)
  const [err, setErr] = useState('')
  const busy = useRef(false)
  const timer = useRef(null)
  const refresh = useCallback(async () => {
    if (busy.current) return
    busy.current = true
    try {
      const s = await ctx.rest('/status', { timeoutMs: 20000 })
      lastStatus = s
      setSt(s)
      setErr('')
    } catch (e) {
      setErr(String(e && e.message ? e.message : e))
    } finally { busy.current = false }
  }, [ctx])
  useEffect(() => {
    let live = true
    // Self-scheduling rather than a fixed interval: the next look is scheduled
    // from the answer that just came back, so a boot in progress is watched once
    // a second and a settled engine is left alone.
    const tick = async () => {
      await refresh()
      if (!live) return
      timer.current = setTimeout(tick, pollDelay(lastStatus))
    }
    tick()
    return () => { live = false; if (timer.current) clearTimeout(timer.current) }
  }, [refresh])
  return [st, err, refresh]
}

function Field({ value, placeholder, onChange, onSubmit, disabled }) {
  return jsx(SearchField, {
    value: value,
    placeholder: placeholder,
    disabled: disabled,
    onChange: (v) => onChange(typeof v === 'string' ? v : (v && v.target && v.target.value) || ''),
    onSubmit: onSubmit,
  })
}

function fmtBytes(n) {
  const v = Number(n) || 0
  if (v < 1024) return v + ' B'
  const units = ['KB', 'MB', 'GB', 'TB']
  let x = v / 1024
  let i = 0
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1 }
  return (x < 10 ? x.toFixed(1) : Math.round(x)) + ' ' + units[i]
}

// A job reports `progress` separately from its log, because "copying" is not a
// measure of anything. `total` can be 0 when the size was never measured, and a
// bar that cannot know its fraction says so instead of drawing a fake one.
function Bar({ done, total, state, label, value }) {
  const known = Number(total) > 0
  const pct = known ? Math.max(0, Math.min(100, (Number(done) / Number(total)) * 100)) : 0
  const cls = 'ah-barfill'
    + (state === 'failed' ? ' failed' : state === 'done' ? ' done' : '')
    + (known ? '' : ' indet')
  return jsxs('div', { className: 'ah-prog', children: [
    jsxs('div', { className: 'ah-progtop', children: [
      jsx('span', { className: 'ah-proglab', children: label || '' }),
      jsx('span', { className: 'ah-progval', children: value || (known ? Math.round(pct) + '%' : '') }),
    ] }),
    jsx('div', { className: 'ah-bar', children:
      jsx('div', { className: cls, style: known ? { width: pct + '%' } : undefined }) }),
  ] })
}

const JOB_STATE_LABEL = {
  waiting: 'waiting', copying: 'copying', linking: 'linking', copied: 'done',
  linked: 'linked', skipped: 'skipped', failed: 'failed',
}

// Shows one bar for the whole job and one per item, so a three-app import reads
// as real progress instead of a bare "2/3".
function JobProgress({ progress }) {
  if (!progress) return null
  const items = progress.apps || []
  const known = Number(progress.total) > 0
  const overall = jsx(Bar, {
    done: progress.done, total: progress.total,
    state: items.length && items.every(i => ['copied', 'linked', 'skipped', 'failed'].includes(i.state)) ? 'done' : '',
    label: progress.label || 'Working…',
    value: known ? `${fmtBytes(progress.done)} / ${fmtBytes(progress.total)}` : fmtBytes(progress.done || 0),
  })
  if (!items.length) return overall
  return jsxs('div', { children: [
    overall,
    jsx('div', { className: 'ah-steps', children: items.map((it, i) =>
      jsxs('div', { className: 'ah-step', children: [
        jsx('span', { className: 'ah-stepname', children: it.name }),
        jsx('span', {
          className: 'ah-stepstate' + (it.state === 'failed' ? ' failed'
            : ['copied', 'linked'].includes(it.state) ? ' done' : ''),
          children: (JOB_STATE_LABEL[it.state] || it.state)
            + (Number(it.total) > 0 && it.state === 'copying'
                ? ` ${Math.round((it.done / it.total) * 100)}%` : ''),
        }),
      ] }, 's' + i)) }),
  ] })
}

// What Fix actually checked, so the answer is a list rather than a shrug. Each
// line says whether it passed, and a failure says whether it can be repaired or
// is genuinely broken — the detail is in the tooltip so the rows stay short.
//
// A check that came back with `next_step` gets a button for it. That is the
// whole point of the step existing: "this app cannot generate" is the normal
// state of a fresh install rather than breakage, it is the one thing standing
// between the app and doing its job, and the machine already knows which folders
// would fix it — so the button asks the plugin which ones those are instead of
// asking a person to type a path.
function HealthReport({ health, app, ctx, onChange }) {
  const [busy, setBusy] = useState(false)
  const [cands, setCands] = useState(null)
  const [note, setNote] = useState('')
  if (!health || !health.checks || !health.checks.length) return null
  const enc = encodeURIComponent(app.id)

  const point = async (folder, force) => {
    setBusy(true)
    try {
      const r = await ctx.rest(`/apps/${enc}/models`, {
        method: 'POST', timeoutMs: 60000,
        body: { action: 'point', folder, force: !!force },
      })
      if (!r || r.ok === false) {
        // A mapping already there is the one refusal worth a second button: an
        // empty leftover and a deliberate mapping look the same from here, so
        // the person decides whether it may be replaced.
        setNote(r && r.error ? String(r.error) : 'Could not point it at those weights.')
        if (r && r.exists) {
          setCands([{ folder, replace: true }])
        }
        return
      }
      const n = (r.models && r.models.count) || 0
      setNote(`Found ${n} weight file(s) — this app can generate now.`)
      setCands(null)
      if (onChange) await onChange()
    } catch (e) {
      setNote('Could not point it at those weights: ' + e)
    } finally { setBusy(false) }
  }

  // The next step, carried out. `action` says which answer applies, and there is
  // more than one because "it stopped" has more than one shape: a card another app
  // is holding gets closed, a broken environment gets repaired, a gated download
  // needs the credentials this machine already holds, weights get pointed at, and
  // some causes have no action at all because the fix is a decision rather than a
  // button. This used to run the weights flow for every button, which offered the
  // wrong fix for four of those five cases.
  const ask = async (check) => {
    const step = (check && check.next_step) || null
    const action = (step && step.action) || 'point_models'
    setBusy(true)
    setNote('')
    try {
      if (action === 'none') {
        setNote((step && step.detail) || (check && check.detail) || 'Nothing to do here.')
        return
      }
      if (action === 'stop_app') {
        const who = (step && step.apps) || []
        if (!who.length) { setNote('Nothing else is holding the card.'); return }
        for (const id of who) {
          await ctx.rest(`/apps/${encodeURIComponent(id)}/stop`,
            { method: 'POST', timeoutMs: 60000 })
        }
        setNote(`Stopped ${who.join(', ')} — the card is free, so this can be tried again.`)
        if (onChange) await onChange()
        return
      }
      if (action === 'fix_app') {
        const r = await ctx.rest(`/apps/${enc}/fix`, { method: 'POST', timeoutMs: 900000 })
        setNote((r && r.ok !== false)
          ? 'Repaired — the environment now has what it was missing.'
          : ('Could not repair it: ' + ((r && r.error) || 'unknown')))
        if (onChange) await onChange()
        return
      }
      if (action === 'credentials') {
        // Seeding is the whole step: the token is one Hermes already holds, so
        // nothing is typed here and the value never passes through this panel.
        const r = await ctx.rest('/credentials/seed', { method: 'POST', timeoutMs: 60000 })
        // A refusal here used to read as "no matching token on this machine yet", which sends
        // somebody to look for a token that is already there. The route's own sentence first.
        const why = actionFailure('Sharing credentials', r)
        if (why) { setNote(why); return }
        const n = (r && r.written && r.written.length) || 0
        setNote(n
          ? `Shared the ${n} credential file(s) this machine already had — try again now.`
          : 'No matching token on this machine yet — add one in Settings, then try again.')
        return
      }
      if (action === 'wait') {
        // Queuing is a real answer here because the card frees on its own: the app
        // that holds it finishes, and nothing has to be thrown away. The queue is
        // in this dashboard's memory, which the backend also says on /waiting.
        const r = await ctx.rest(`/apps/${enc}/wait`,
          { method: 'POST', timeoutMs: 30000, body: { action: 'launch' } })
        // A refusal read as "Queued at position 1" is a promise the backend did not make.
        const why = actionFailure('Queuing it', r)
        if (why) { setNote(why); return }
        const at = (r && r.position) || 1
        setNote((r && r.already)
          ? 'Already queued — it starts on its own when the card has room.'
          : `Queued${at > 1 ? ` at position ${at}` : ''} — it starts on its own when the `
            + 'card has room. Watch it (and take it back) on the Terminal.')
        return
      }
      if (action === 'shelf') {
        openSystemWindow()
        setNote('Opened System — the weight shelf there holds what this machine has, and can pull what it lacks.')
        return
      }
      // Weights, and any action this panel does not know: picking a folder that
      // already holds weights is the commonest answer, and harmless if wrong.
      const r = await ctx.rest(`/apps/${enc}/models`, { timeoutMs: 60000 })
      const list = ((r && r.candidates) || []).filter(c => c && c.folder)
      if (!list.length) {
        setNote('No model weights anywhere on this machine yet — download one first.')
        return
      }
      // One library is the usual answer (this is a machine that keeps its
      // weights in one place), so it is applied rather than offered.
      if (list.length === 1) { setBusy(false); return point(list[0].folder, false) }
      setCands(list)
    } catch (e) {
      setNote('Could not do that: ' + e)
    } finally { setBusy(false) }
  }

  // The account of why it stopped, when the log has one, goes above the checks:
  // someone opens this panel because something did not work, and the app's own
  // sentence about that is worth more than any structural check underneath it. The
  // sentence is quoted rather than paraphrased because the paraphrasing is where
  // readers disagree — and because a person pasting this into a search needs the
  // words the program actually printed.
  const f = health.failure
  const failure = (f && f.why)
    ? jsxs('div', { className: 'ah-fail', children: [
        jsx('div', { className: 'ah-failtitle', children: f.title || 'It stopped' }),
        jsx('div', { className: 'ah-failwhy', children: f.why }),
        (f.evidence && f.evidence.text)
          ? jsx('div', { className: 'ah-failev',
              title: 'log line ' + f.evidence.line, children: f.evidence.text })
          : null,
        (f.also && f.also.length)
          ? jsx('div', { className: 'ah-failalso', children:
              'also in this log: ' + f.also.map(a => a.cause).join(', ') })
          : null,
        (f.fix && f.fix.action && f.fix.action !== 'none')
          ? jsx('div', { className: 'ah-failrow', children: [
              jsx('button', {
                className: 'ah-btn ah-stepbtn', disabled: busy,
                onClick: () => ask({ next_step: f.fix, detail: f.fix.detail }),
                children: busy ? 'Working…' : (f.fix.label || 'Fix this'),
              }),
              (f.fix.detail)
                ? jsx('span', { className: 'ah-sub', children: f.fix.detail })
                : null,
            ] })
          : null,
        // A second answer, when the cause has two that are both right: closing the
        // app that holds the card frees it now and throws that work away, waiting
        // starts this one when the holder finishes by itself. The panel cannot know
        // which is meant, and picking one for the reader is how a button tells a lie.
        (f.fix && f.fix.alt && f.fix.alt.action)
          ? jsx('div', { className: 'ah-failrow', children: [
              jsx('button', {
                className: 'ah-btn ah-stepbtn', disabled: busy,
                onClick: () => ask({ next_step: f.fix.alt, detail: f.fix.alt.detail }),
                children: f.fix.alt.label || 'Or wait',
              }),
              (f.fix.alt.detail)
                ? jsx('span', { className: 'ah-sub', children: f.fix.alt.detail })
                : null,
            ] })
          : null,
      ] })
    : null

  return jsxs('div', { children: [
    failure,
    jsx('div', { className: 'ah-steps', children: health.checks.map((c, i) =>
      jsxs('div', { className: 'ah-step', title: c.detail, children: [
        jsx('span', { className: 'ah-stepname', children: c.name }),
        jsx('span', {
          className: 'ah-stepstate' + (c.ok ? ' done' : (c.informational ? '' : ' failed')),
          children: c.ok ? 'ok'
            : (c.informational ? 'note' : (c.fixable ? 'repairable' : 'broken')),
        }),
        (c.next_step && c.next_step.action)
          ? jsx('button', {
              className: 'ah-btn ah-stepbtn', disabled: busy, onClick: () => ask(c),
              children: busy ? 'Working…' : (c.next_step.label || 'Fix this'),
            }, 'b' + i)
          : null,
      ] }, 'h' + i)) }),
    (cands && cands.length)
      ? jsx('div', { className: 'ah-choices', children: cands.map((c, i) =>
          jsx('button', {
            className: 'ah-choice', key: 'm' + i, disabled: busy, onClick: () => point(c.folder, c.replace),
            children: c.replace ? `Replace the mapping with ${c.folder}`
              : `${c.folder}${c.total ? ` — ${c.total} file(s)` : ''}${(c.apps && c.apps.length) ? ` (used by ${c.apps.join(', ')})` : ''}`,
          }, 'm' + i)) })
      : null,
    note ? jsx('div', { className: 'ah-note', children: note }) : null,
  ] })
}

// Opening the viewer is its own function because two places do it: the Launch
// button, and a click on an app that is already running.
function openViewer(ctx, app, url) {
  viewer.set({ id: app.id, name: app.name || app.id, url: url || app.url || '' })
  if (hasWorkspaceApi()) { openViewerWindow(); return }
  try {
    if (host && typeof host.revealPane === 'function') host.revealPane(ID + ':athena-viewer')
  } catch { /* older desktops: the pane stays a background tab */ }
}

function Icon({ src, name }) {
  const [bad, setBad] = useState(false)
  if (!src || bad) {
    const ch = String(name || '?').trim().charAt(0).toUpperCase()
    return jsx('div', { className: 'ah-icoph', children: ch })
  }
  return jsx('img', { className: 'ah-ico', src: src, alt: '', onError: () => setBad(true) })
}

/* ---------------------------------------------------------------- header */

/* A home path with the boilerplate folded away, so the line fits. */
function shortHomePath(home) {
  return home
    ? String(home).replace(/^.*[\\/]AppData[\\/]Local[\\/]hermes/i, '…') : ''
}

/* What the header says about the engine. A plain function of the last status so
   it can be asserted: a held-but-silent port is a boot in progress and says
   "starting…" — the header used to say "stopped" over an engine that was coming
   up, which is the opposite of what a person needs to read while they wait. */
function engineLine(st, err) {
  if (!st) return err ? 'engine status unavailable' : 'stopped'
  if (st.ready) return `ready · port ${st.port} · ${shortHomePath(st.home)}`
  if (st.starting || st.running) return 'starting…' + waited(st && st.starting_ms)
  return st.last_error || 'stopped'
}

/* How long the wait has been, in the only unit that helps somebody watching it: seconds.
   Under a second there is nothing to read, and a number that was never measured is not
   invented — an engine this process did not start has no spawn time, so the header
   says "starting…" and stops there rather than guessing. */
function waited(ms) {
  if (typeof ms !== 'number' || !isFinite(ms) || ms < 1000) return ''
  return ` ${Math.round(ms / 1000)}s`
}

function Header({ ctx, st, err, refresh }) {
  const [working, setWorking] = useState('')
  const ready = !!(st && st.ready)
  // The button keeps the narrower meaning: Stop is offered only for an engine
  // this process spawned. A held-but-silent port can be something that is not
  // ours, and Stop reaps whatever holds it.
  const running = !!(st && st.running)
  const act = async (path, label) => {
    setWorking(label)
    try {
      const res = await ctx.rest(path, { method: 'POST', timeoutMs: 180000 })
      const why = actionFailure(label, res)
      if (why) notify(ctx, 'error', why)
      await refresh()
    } catch (e) { notify(ctx, 'error', `${label}: ${e}`) } finally { setWorking('') }
  }
  return jsxs('div', { className: 'ah-head', children: [
    jsx('div', { className: 'ah-dot' + (ready ? ' on' : (err ? ' err' : '')) }),
    jsxs('div', { children: [
      jsx('div', { className: 'ah-title', children: 'Athena' }),
      jsx('div', { className: 'ah-sub', title: (st && st.last_error) || '',
        children: engineLine(st, err) }),
    ] }),
    jsx('div', { className: 'ah-grow' }),
    running
      ? jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!working,
          onClick: () => act('/engine/stop', 'Stop'),
          children: working === 'Stop' ? 'Stopping…' : 'Stop' })
      : jsx(Button, { size: 'micro', disabled: !!working,
          onClick: () => act('/engine/start', 'Start'),
          children: working === 'Start' ? 'Starting…' : 'Start' }),
    jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!working,
      onClick: () => act('/engine/restart', 'Restart'), children: 'Restart' }),
  ] })
}

/* ---------------------------------------------------------------- apps */

/* What an app asks you for, as one badge, read off the scan the backend keeps.
   A plain function of the data on purpose: cards render constantly and a render
   must never walk a folder, so this only ever *words* an answer that is already
   on the payload — and a test can hand it a fabricated answer.
   `local === null` (never read) is a different badge from `local === false`:
   "we have not looked" and "we looked, and it calls out" are not the same claim,
   and a badge that blurs them teaches people to distrust all of them. */
function keyBadge(keys) {
  if (!keys || keys.scanned !== true) {
    return { cls: '', text: 'scripts not read',
      title: 'Athena has not read this app\u2019s scripts yet. Press \u201cWhat it needs\u201d to read them \u2014 it is a directory walk, nothing is sent anywhere.' }
  }
  const total = keys.keys_total || 0
  const held = keys.held_count || 0
  const cloud = keys.cloud_total || 0
  const names = (keys.keys || []).map((k) => k.name).join(', ')
  if (!total && !cloud) {
    return keys.complete
      ? { cls: 'run', text: 'runs here',
        title: 'No key and no cloud endpoint anywhere in its scripts \u2014 this one runs on your box.' }
      : { cls: '', text: 'partly read',
        title: 'Nothing found, but a file was too big to read, so this is not a clean answer.' }
  }
  if (total && !cloud && held === total) {
    return { cls: 'run', text: 'keys ready',
      title: `${total === 1 ? 'Its scripts read one key' : 'Its scripts read ' + total + ' keys'} (${names}), and Hermes already answers ${total === 1 ? 'it' : 'every one'}.` }
  }
  if (!total && cloud) {
    return { cls: 'need', text: 'calls out',
      title: `${cloud === 1 ? 'One cloud endpoint is' : cloud + ' cloud endpoints are'} written into its scripts with no key named \u2014 it still wants an account.` }
  }
  const missing = total - held
  return { cls: 'need',
    text: missing > 0 ? `needs ${missing} key${missing === 1 ? '' : 's'}` : 'calls out',
    title: `Its scripts read ${names}. Hermes already answers ${held} of ${total}${cloud ? ', and it also names a cloud endpoint' : ''}.` }
}

/* The filter is about apps that are *here*: the scan reads folders on this
   machine, so a store row has nothing to filter on yet. Kept separate from the
   badge so the list can filter by the same rule the badge words. */
function keysMatch(keys, only) {
  if (!only) return true
  const k = keys || {}
  if (only === 'need') {
    return k.scanned === true
      && ((k.keys_total || 0) > (k.held_count || 0) || (k.cloud_total || 0) > 0)
  }
  if (only === 'local') return k.scanned === true && k.local === true
  return true
}

/* What state this app is in, in the fewest words that are still true.
   Pure, and top-level, so the harness can hand it any row the panel can receive.

   It used to be a bare `scripts not read` line on every card, plus a separate
   `running` badge — so the one fact worth seeing at a glance was two pieces of
   text competing with nine buttons. One chip now, and the words come from
   `keyBadge` so a card and the "needs a key" filter can never disagree about
   what an app is asking for. Nothing here is inferred: each word is read off a
   field that arrived from disk. */
function appStateWords(app) {
  if (!app) return { cls: '', text: '', title: '' }
  if (app.running) {
    // The port is the useful half of "running": it is what Open opens, and what
    // a second window has to be pointed at. Read from the url the engine reported.
    const port = (String(app.url || '').match(/:(\d{2,5})\b/) || [])[1]
    return { cls: 'run', text: port ? `running \u00b7 :${port}` : 'running',
      title: app.url ? `Up now at ${app.url}` : 'Running, with no web address reported.' }
  }
  if (app.manifest === false) {
    return { cls: 'need', text: 'no launcher',
      title: 'There is no launcher script in this folder, so the engine has nothing to start. Fix it, or install it from the store.' }
  }
  const b = keyBadge(app.keys)
  if (b.text === 'scripts not read') {
    return { cls: '', text: 'nothing read yet', title: b.title }
  }
  return b
}

/* What this app can be asked to do right now, in the order a person wants it.

   The card had ten buttons on every app regardless of state, which made the app
   description and its tags the quietest things on it. `primary` is the one thing
   you came to do, `second` is the other thing you reach for at the same moment,
   and the rest live behind the menu — nothing is hidden about what an action is,
   it is just not shouting. `needs` is false for anything that cannot work in this
   state, so an action that would only refuse is not offered at all. */
function appActions(app) {
  const running = !!(app && app.running)
  const noLauncher = !!(app && app.manifest === false)
  const scanned = !!(app && app.keys && app.keys.scanned)
  const primary = running
    ? { act: 'open', label: 'Open', busy: 'open' }
    : (noLauncher
      ? { act: 'install', label: 'Install', busy: 'install' }
      : { act: 'launch', label: 'Launch', busy: 'launch' })
  // Stop is the second button while it runs, because that is the moment someone
  // wants it; while it does not run, Terminal is, because that is how you watch
  // whatever you just asked for.
  const second = running
    ? { act: 'stop', label: 'Stop', busy: 'stop' }
    : { act: 'terminal', label: 'Terminal' }
  const more = []
  if (running) more.push({ act: 'terminal', label: 'Terminal' })
  more.push({ act: 'keys', busy: 'keys',
    label: scanned ? 'What it needs' : 'Read its scripts' })
  more.push({ act: 'smoke', busy: 'smoke', label: 'Test run' })
  if (!running && !noLauncher) more.push({ act: 'install', busy: 'install', label: 'Install' })
  more.push({ act: 'update', busy: 'update', label: 'Update' })
  more.push({ act: 'update-check', busy: 'update-check', label: 'Check for updates' })
  more.push({ act: 'fix', busy: 'fix', label: 'Fix' })
  more.push({ act: 'publish', busy: 'publish', label: 'Publish' })
  more.push({ act: 'delete', busy: 'delete', label: 'Delete\u2026', danger: true })
  return { primary, second, more }
}

/* One action word, one handler. Kept as a plain function so the harness can build
   the same table out of stubs and click through it — the card's buttons and the
   tests then run the same mapping rather than two copies of it that agree today. */
function appHandlers(h) {
  return {
    launch: () => h.run('launch'),
    stop: () => h.run('stop'),
    update: () => h.run('update'),
    open: h.open,
    terminal: h.openTerminal,
    keys: h.readKeys,
    smoke: h.smoke,
    install: h.install,
    'update-check': h.checkUpdate,
    fix: h.fix,
    publish: h.publish,
    delete: h.remove,
  }
}

function AppCard({ ctx, app, onChange, row, menuOpen }) {
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')
  const [progress, setProgress] = useState('')
  const [choices, setChoices] = useState(null)
  const [report, setReport] = useState(null)
  // The update check is a real `git fetch`, so its answer is held here and only
  // ever refreshed by an explicit click — never on mount, never on a timer.
  const [update, setUpdate] = useState(null)
  // The menu is per-card and closes itself on any pick: a menu that stays open
  // after the action it described has started is a menu that looks broken.
  const [menu, setMenu] = useState(!!menuOpen)

  // The engine reports whether a script actually started, and that answer was
  // being thrown away — so a launch that failed silently looked like a button
  // that did nothing. A launch that works hands back the address its web
  // interface came up on, and that is what the viewer opens.
  const run = async (action, choice) => {
    setBusy(action)
    setMsg('')
    setProgress('')
    setChoices(null)
    try {
      const r = await ctx.rest(`/apps/${encodeURIComponent(app.id)}/${action}`,
        { method: 'POST', timeoutMs: 300000,
          body: choice ? { href: choice.href, input: choice.input || {} } : undefined })
      if (r && r.ok) {
        // Warnings, not failures: a bind that had to find another port, a card
        // with little left on it, a drive that is getting full. Each is read off
        // real numbers at the moment it matters (the port from the app's own
        // output, the rest before the work started), and each belongs on the
        // card, where the click was — not only at the bottom of the terminal.
        const head = (r && (r.port_warning || r.vram_warning || r.disk_warning)) || ''
        if (action === 'launch') {
          // Open the viewer either way: an app that is still booting has no
          // address yet, and the viewer is where you watch it come up.
          openViewer(ctx, app, r.url || '')
          setMsg(head || (r.url ? '' : (r.url_note || '')))
        } else {
          const state = (r.run && r.run.state) || 'started'
          const said = action === 'stop' ? 'Stopped.'
            : state === 'finished' ? 'Ran to completion — the output is on its Terminal.'
            : `Started (${state}). Output is on its Terminal.`
          setMsg(head ? `${said} ${head}` : said)
        }
        notify(ctx, 'info', `${app.name || app.id}: ${action}`)
      } else if (r && r.needs_choice) {
        // Not a failure. This app marks no default action and keeps its runnable
        // scripts inside submenus, so nothing can guess which one to run — its
        // own menu is the only honest answer.
        setChoices(r.choices || [])
        setMsg(r.choices && r.choices.length
          ? 'This app has no default — choose which one to run.'
          : (r.error || 'This app offers nothing to run.'))
      } else {
        // A refusal the backend decided on carries its own sentence (`message`),
        // which is the whole point of it: "C: has 0.4 GB free" is actionable and
        // "the engine did not report a result" is not.
        const why = (r && (r.error || (r.run && r.run.error) || r.message))
          || 'the engine did not report a result'
        const why2 = (r && (r.port_failure || r.port_warning || r.vram_warning)) || ''
        setMsg(`${action} failed: ${why}${why2 ? ' — ' + why2 : ''}`)
        notify(ctx, 'error', `${app.name || app.id}: ${action} failed`)
      }
      await onChange()
    } catch (e) {
      setMsg(`${action} failed: ${e}`)
      notify(ctx, 'error', `${app.name || app.id}: ${e}`)
    } finally { setBusy('') }
  }
  const open = async () => {
    setBusy('open')
    try {
      const s = await ctx.rest(`/apps/${encodeURIComponent(app.id)}/status`, { timeoutMs: 20000 })
      if (!s || !s.url) { notify(ctx, 'info', 'This app has not opened a web interface.'); return }
      openViewer(ctx, app, s.url)
    } catch (e) { notify(ctx, 'error', `Open: ${e}`) } finally { setBusy('') }
  }
  // Deleting takes the app's downloaded models with it, which is often the most
  // expensive thing on the disk, so it is confirmed rather than done on a click.
  const remove = async () => {
    const label = app.name || app.id
    const ok = window.confirm(
      `Delete ${label}?\n\nThis removes the app folder and everything inside it — ` +
      `including any models it downloaded. This cannot be undone.`)
    if (!ok) return
    setBusy('delete')
    setMsg('')
    setProgress('Deleting…')
    try {
      const r = await ctx.rest(`/apps/${encodeURIComponent(app.id)}`,
        { method: 'DELETE', timeoutMs: 60000 })
      if (!r || !r.ok) {
        setMsg(`Delete failed: ${(r && r.error) || 'unknown error'}`)
        setProgress('')
        return
      }
      // Deleting is slow and runs server-side, so progress is polled rather
      // than the panel holding a request open and looking frozen.
      const out = await awaitJob(ctx, r.job, {
        onTick: (j) => setProgress((j && j.tail) ? j.tail.split('\n').slice(-1)[0] : 'Deleting…'),
      })
      if (out.ok) {
        const note = out.result && out.result.note
        setMsg(note ? `${label}: link removed, original left in place.` : `${label} deleted.`)
        setProgress('')
        notify(ctx, 'info', note ? `Removed ${label}'s link` : `Deleted ${label}`)
        await onChange()
      } else {
        setMsg(`Delete failed: ${out.error}`)
        setProgress('')
        notify(ctx, 'error', `Delete failed: ${out.error}`)
      }
    } catch (e) {
      setMsg(`Delete failed: ${e}`)
      setProgress('')
    } finally { setBusy('') }
  }
  // Fix exists because re-installing from the store to repair a path that points
  // at the wrong folder is not a repair — it is a re-download, of an app that
  // may be tens of gigabytes and is already sitting right here. It verifies the
  // app, repairs everything with a repair, and then provisions — running the
  // app's own install when nothing was ever installed into its environment, or
  // when its install has never run in this copy. Fix is expected to fix, so it
  // does not stop at a diagnosis it declined to act on.
  const enc = encodeURIComponent(app.id)

  // Long actions belong on the terminal, not behind a button that just says "…":
  // a fix and an install both write continuously and take minutes, and watching
  // them is the whole point of having asked. This is also the only thing that
  // puts the Terminal tab in the bar — the tab is not standing furniture, it
  // exists because someone asked this app for its terminal.
  const openTerminal = () => { termApp.set(app.id); tab.set('terminal') }

  // Asking the backend whether this app's repository has moved on is a real
  // `git fetch` over the network — the route is named for that cost — so it
  // happens only when the user asks, never on mount, never on a timer, and
  // never once per app on render. The answer is kept on the card, so a
  // re-render does not ask again.
  // Where the app has been, and the put-back currently running. Kept on the card
  // so opening the history twice does not ask twice, and so the confirm a person
  // sees is about the same list they were looking at.
  const [hist, setHist] = useState(null)
  const [histBusy, setHistBusy] = useState('')

  const checkUpdate = async () => {
    setBusy('update-check')
    try {
      const r = await ctx.rest(`/apps/${enc}/update-check`, { timeoutMs: 120000 })
      setUpdate(r || { ok: false })
    } catch (e) {
      setUpdate({ ok: false, error: String(e) })
    } finally { setBusy('') }
  }

  // The update itself is the app's own update.js, run through the same action
  // runner Launch and Fix use. It writes continuously, so the user is sent to
  // the terminal where that output lands, exactly as Fix does.
  const updateNow = () => { openTerminal(); return run('update') }

  // History needs no network: a reflog read and a HEAD read. So it is fetched
  // when asked and never on mount, and it answers offline as well as online.
  const showHistory = async () => {
    setHistBusy('load')
    try {
      const r = await ctx.rest(`/apps/${enc}/history`, { timeoutMs: 60000 })
      setHist(r || { ok: false })
    } catch (e) {
      setHist({ ok: false, error: String(e) })
    } finally { setHistBusy('') }
  }

  // Putting an app back is a change to files on disk, so it is confirmed out loud
  // once — as Delete and Publish are — with what it costs spelled out first. The
  // output goes to the same Terminal an update does, because it runs the app's own
  // re-read afterwards and that writes.
  const putBack = async (commit) => {
    const c = commit || {}
    const short = c.short || String(c.sha || '').slice(0, 12)
    const label = app.name || app.id
    const cost = []
    if (hist && hist.dirty) cost.push(`${hist.dirty} file(s) with changes that are not in a commit`)
    if (hist && hist.ahead) cost.push(`${hist.ahead} commit(s) made in this folder`)
    const ok = window.confirm(
      `Put ${label} back to ${short}?\n\n` +
      (cost.length ? `This gives up ${cost.join(' and ')}. ` : '') +
      `The app's files go back to what that commit says, and it will be behind ` +
      `its remote again — an Update brings it forward.\n\n` +
      (c.what ? `That commit: ${c.what}` : ''))
    if (!ok) return
    setHistBusy(c.sha)
    openTerminal()
    let job = ''
    try {
      const r = await ctx.rest(`/apps/${enc}/revert`, { method: 'POST', timeoutMs: 60000,
        body: { sha: c.sha } })
      if (!r || !r.ok) {
        setMsg('Put back failed: ' + ((r && r.error) || 'unknown'))
        notify(ctx, 'error', `${label}: put back failed`)
        return
      }
      job = r.job
      activeJob.set({ app: app.id, id: r.job })
      const out = await awaitJob(ctx, r.job, { onTick: (j) => setProgress(j.tail || '') })
      setProgress('')
      if (!out.ok) {
        const why = out.error || 'unknown'
        setMsg('Put back failed: ' + why)
        notify(ctx, 'error', `${label}: put back failed — ${why}`)
      } else {
        setMsg(`${label} is back on ${short}.`)
        notify(ctx, 'info', `${label} is back on ${short}`)
      }
    } catch (e) {
      setMsg('Put back failed: ' + String(e))
      notify(ctx, 'error', `${label}: put back failed`)
    } finally {
      setHistBusy('')
      setHist(null)
      checkUpdate()
    }
  }

  const fix = async () => {
    setBusy('fix')
    setMsg('Checking…')
    setProgress('')
    setChoices(null)
    openTerminal()
    let installing = false
    try {
      const r = await ctx.rest(`/apps/${enc}/fix`, { method: 'POST', timeoutMs: 60000 })
      if (!r || !r.ok) {
        setMsg('Fix failed: ' + ((r && r.error) || 'unknown'))
        notify(ctx, 'error', `${app.name || app.id}: fix failed`)
        return
      }
      activeJob.set({ app: app.id, id: r.job })
      const out = await awaitJob(ctx, r.job, { onTick: (j) => setProgress(j.tail || '') })
      setProgress('')
      const res = out.result || {}
      if (res.health) setReport(res.health)
      const repaired = res.fixed || []
      if (!out.ok) {
        setMsg('Fix failed: ' + (out.error || 'unknown'))
        notify(ctx, 'error', `${app.name || app.id}: fix failed`)
      } else if (res.health && res.health.ok) {
        setMsg('Everything checks out.')
        notify(ctx, 'info', `${app.name || app.id}: no problems found`)
      } else if (res.install_started) {
        // A fix that escalated to provisioning is not finished when the job
        // ends — the build it started is still running. Stay where the output is.
        installing = true
        setMsg('Repaired, and its own install is running — watching it on its Terminal.')
        notify(ctx, 'info', `${app.name || app.id}: installing`)
      } else if (repaired.length) {
        setMsg('Repaired: ' + repaired.join('; ') + '.')
        notify(ctx, 'info', `${app.name || app.id}: repaired`)
      } else {
        setMsg('Checked. Something still needs attention — see the report above.')
      }
      await onChange()
    } catch (e) {
      setMsg('Fix failed: ' + e)
      notify(ctx, 'error', `${app.name || app.id}: ${e}`)
    } finally {
      activeJob.set(null)
      setBusy('')
      // Back to the list once there is nothing left to watch: the answer to
      // "did it work" is on the card, not at the bottom of a log. Leaving the app on the
      // terminal atom as well is what used to leak the tab — the job was over and the tab
      // stayed, showing the log of an app nobody had asked to watch.
      if (!installing) { termApp.set(null); tab.set('apps') }
    }
    if (installing) await watchInstall()
  }

  const install = async () => {
    setBusy('install')
    setMsg('')
    setProgress('')
    setChoices(null)
    openTerminal()
    try {
      const r = await ctx.rest(`/apps/${enc}/install`, { method: 'POST', timeoutMs: 60000 })
      if (!r || !r.ok) {
        setMsg('Install failed: ' + ((r && r.error) || 'unknown'))
        notify(ctx, 'error', `${app.name || app.id}: install failed`)
        tab.set('apps')
        return
      }
      setMsg('Installing — the output is on its Terminal.')
      notify(ctx, 'info', `${app.name || app.id}: installing`)
      await watchInstall()
    } catch (e) {
      setMsg('Install failed: ' + e)
      tab.set('apps')
    } finally { setBusy('') }
  }

  // The one action on this card that leaves the machine: it rewrites the
  // store's branches from what is installed here and pushes them to GitHub with
  // Hermes' own login. So it is a button rather than a side effect of something
  // else, it is confirmed out loud once, and its output goes to the same
  // Terminal a build does — a clone, a rewrite and a push are minutes, and
  // minutes belong somewhere watchable.
  const publish = async () => {
    const label = app.name || app.id
    const sure = window.confirm(
      `Publish ${label} to the store?\n\nThis rebuilds every app branch in the store ` +
      `and pushes them to GitHub using the GitHub login Hermes already has.\n\n` +
      `It also refreshes the store's index, so the store keeps mirroring this machine.`)
    if (!sure) return
    setBusy('publish')
    setMsg('')
    setProgress('Publishing…')
    setChoices(null)
    openTerminal()
    try {
      const r = await ctx.rest(`/apps/${enc}/publish`, { method: 'POST', timeoutMs: 60000 })
      if (!r || !r.ok) {
        // A refusal here is a sentence, not a status code: "no GitHub credential
        // is available" is the whole answer, and it is on the card.
        const why = (r && (r.error || r.message)) || 'unknown'
        setMsg('Publish failed: ' + why)
        setProgress('')
        notify(ctx, 'error', `${label}: publish failed — ${why}`)
        return
      }
      activeJob.set({ app: app.id, id: r.job })
      const out = await awaitJob(ctx, r.job, { onTick: (j) => setProgress(j.tail || '') })
      activeJob.set(null)
      setProgress('')
      if (out.ok) {
        setMsg('Published — ' + (r.branch ? r.branch + ' and the index are' : 'the branches are')
               + ' on GitHub' + (r.store ? ' (' + r.store + ')' : '') + '.')
        notify(ctx, 'info', `${label}: published`)
        tab.set('apps')
      } else {
        setMsg('Publish failed: ' + (out.error || 'unknown'))
        notify(ctx, 'error', `${label}: publish failed`)
        openTerminal()   // the reason is in the output, so stay where the output is
      }
      await onChange()
    } catch (e) {
      setMsg('Publish failed: ' + e)
      setProgress('')
      activeJob.set(null)
    } finally { setBusy('') }
  }

  // A launcher that prints a URL and dies is a lie a green card would repeat.
  // Test run launches the app, waits for its port and fetches it, so "it works"
  // is something we saw rather than something the script claimed.
  const smoke = async () => {
    setBusy('smoke')
    setMsg('Testing — launching it and waiting for it to answer…')
    try {
      const r = await ctx.rest(`/apps/${enc}/smoke`, { method: 'POST', timeoutMs: 100000 })
      if (r && r.ok) {
        setMsg(`Test run passed: ${r.url} answered HTTP ${r.http}${r.seconds_waited ? ` in ${Math.round(r.seconds_waited)}s` : ''}.`)
        notify(ctx, 'info', `${app.name || app.id}: answered HTTP ${r.http}`)
      } else {
        const why = (r && (r.error || r.note)) || 'it did not come up'
        setMsg('Test run failed: ' + why)
        notify(ctx, 'error', `${app.name || app.id}: ${why}`)
        openTerminal()   // the reason is in the output, so show the output
      }
      await onChange()
    } catch (e) {
      setMsg('Test run failed: ' + e)
      notify(ctx, 'error', `${app.name || app.id}: ${e}`)
    } finally { setBusy('') }
  }

  // Installs run inside the engine, not inside a request, so the only honest
  // "finished" is the script leaving the engine's list of running scripts. It is
  // also listed *late* — the engine reports nothing for the first several
  // seconds of a cold script — so an empty poll before it has ever appeared
  // means "not started yet", not "done". Waiting on a guess would either cut a
  // build off or hang forever.
  const watchInstall = async (limitMs = 45 * 60 * 1000) => {
    const step = 2000
    let seen = false
    let quiet = 0
    for (let waited = 0; waited < limitMs; waited += step) {
      await sleep(step)
      let s = null
      try { s = await ctx.rest(`/apps/${enc}/status`, { timeoutMs: 20000 }) } catch (e) { s = null }
      if (s && s.running) { seen = true; quiet = 0 }
      else if (seen) { quiet += 1; if (quiet >= 2) break }
      else if (waited > 90000) break
    }
    setMsg(seen ? 'Install finished.' : 'Install did not appear to start — see its Terminal.')
    await onChange()
    tab.set('apps')
  }

  // Reading the app's own scripts. A directory walk on this machine and nothing
  // else: no network, no account, no value read out of any file — which is why it
  // is allowed to be a button on a card that someone presses without thinking.
  const readKeys = async () => {
    setBusy('keys')
    setMsg('')
    try {
      const r = await ctx.rest(`/apps/${encodeURIComponent(app.id)}/keys?refresh=1`,
        { timeoutMs: 60000 })
      if (r && r.ok && r.scanned !== false) {
        setMsg(r.local === true
          ? 'Read its scripts: no key and no cloud endpoint — it runs on this box.'
          : (r.keys_total
            ? `Read its scripts: it names ${r.keys_total} key${r.keys_total === 1 ? '' : 's'}`
              + `, and Hermes answers ${r.held_count || 0}.`
            : 'Read its scripts: it calls a cloud endpoint with no key named.'))
      } else {
        setMsg('Could not read its scripts: ' + ((r && r.error) || 'unknown error'))
      }
      await onChange()
    } catch (e) {
      setMsg('Could not read its scripts: ' + e)
    } finally { setBusy('') }
  }
  const st = appStateWords(app)
  const acts = appActions(app)
  const ACTS = appHandlers({ run, open, openTerminal, readKeys, smoke, install,
    checkUpdate, fix, publish, remove })
  const pick = (a) => { setMenu(false); const fn = ACTS[a.act]; if (fn) fn() }
  // What each action says while it is the one running. Kept beside ACTS so the
  // label and the handler cannot drift apart.
  const BUSY = { keys: 'Reading\u2026', smoke: 'Testing\u2026', 'update-check': 'Checking\u2026',
    update: 'Updating\u2026', publish: 'Publishing\u2026', delete: 'Deleting\u2026' }
  const busyWord = (a) => (busy && busy === a.busy ? (BUSY[a.busy] || '\u2026') : a.label)
  // Anything that could not work in this state is not offered: a button whose
  // only possible answer is a refusal is worse than no button.
  const off = (a) => !!busy || (a.act === 'smoke' && !!app.running)
    || (a.act === 'install' && !!app.running)
  // One component, two shells. The row layout is the same card laid out
  // horizontally — same state chip, same primary verb, same menu, same handlers —
  // because a second renderer for the same app is a second place to be wrong.
  return jsxs('div', { className: 'ah-card' + (row ? ' ah-lrow' : ''),
    // The agent names an app by id or by name, so the card answers to both — the
    // spotlight finds it either way. Space-separated because that is what `~=`
    // matches, which keeps this one attribute instead of two.
    'data-athena-spot': 'app:' + app.id + (app.name ? ' app:' + app.name : ''),
    children: [
    jsxs('div', { className: 'ah-cartop', children: [
      jsx(Icon, { src: app.icon, name: app.name || app.id }),
      jsxs('div', { style: { minWidth: 0 }, children: [
        jsxs('h4', { children: [
          jsx('span', { children: app.name || app.id }),
        ] }),
        jsx('div', { className: 'ah-repo', children: app.id }),
        app.running && app.url
          ? jsx('div', { className: 'ah-repo ah-live', children: app.url })
          : null,
      ] }),
    ] }),
    jsx('p', { className: row ? 'ah-clip' : '', children: app.description || 'No description.' }),
    (app.tags && app.tags.length)
      ? jsx('div', { className: 'ah-row', children: app.tags.slice(0, 4).map((t, i) =>
          jsx('span', { className: 'ah-badge', children: '#' + t }, 't' + i)) })
      : null,
    // A launch that failed offers its own repair right here. Being sent back to
    // the store to install again — in order to fix a path pointing at the wrong
    // folder — is the opposite of helpful, so the fix is one click away on the
    // card that just failed.
    msg
      ? jsxs('div', {
          className: 'ah-sub' + (/failed/.test(msg) ? ' ah-warn' : ''),
          style: { display: 'flex', alignItems: 'baseline', gap: 8 },
          children: [
            jsx('span', { style: { flex: '1 1 auto' }, children: msg }),
            /failed/.test(msg)
              ? jsx('button', { className: 'ah-btn', disabled: !!busy, onClick: fix,
                  children: busy === 'fix' ? 'Checking…' : 'Fix it' })
              : null,
          ],
        })
      : null,
    report ? jsx(HealthReport, { health: report, app, ctx, onChange }) : null,
    choices && choices.length
      ? jsxs('div', { className: 'ah-choices', children: choices.map((c, i) => {
          // 33 entries share a handful of submenus, so the path is printed only
          // where it changes — as a heading for the group below it.
          const parents = (c.path || []).slice(0, -1)
          const prev = i ? choices[i - 1].path || [] : []
          const samePath = parents.join('/') === prev.slice(0, -1).join('/')
          return jsxs('button', {
            className: 'ah-choice',
            key: 'c' + i,
            disabled: !!busy,
            title: (c.path || []).join(' › '),
            onClick: () => run('launch', c),
            children: [
              parents.length && !samePath
                ? jsx('span', { className: 'ah-choicepath', key: 'p', children: parents.join(' › ') })
                : null,
              jsx('span', { className: 'ah-choicetext', key: 't', children: c.text }),
            ],
          }, 'c' + i)
        }) })
      : null,
    progress ? jsx(AutoTerm, { text: progress }) : null,
    // The check's answer, kept quiet unless there is genuinely something to
    // update to. A failed check is almost always "offline right now", so it is
    // stated softly — never as an alarm, and never when nothing was asked.
    (update && update.ok && update.is_repo && update.behind > 0)
      ? jsxs('div', { className: 'ah-sub',
          style: { display: 'flex', alignItems: 'baseline', gap: 8 }, children: [
          jsx('span', { style: { flex: '1 1 auto' }, children:
            'Update available — ' + update.behind
            + (update.behind === 1 ? ' commit behind ' : ' commits behind ')
            + (update.branch || 'upstream') + '.' }),
          jsx(Button, { size: 'micro', disabled: !!busy, onClick: updateNow,
            children: busy === 'update' ? 'Updating…' : 'Update now' }),
        ] })
      : null,
    (update && update.ok && update.is_repo && update.behind === 0)
      ? jsx('div', { className: 'ah-sub', children:
          'Up to date with ' + (update.branch || 'upstream') + '.' })
      : null,
    (update && update.ok === false)
      ? jsx('div', { className: 'ah-sub', children:
          'Could not check for updates' + (update.offline ? ' — offline' : '') + '.' })
      : null,
    // The state, once, in one chip — running and what-it-needs were two badges
    // saying overlapping things next to nine buttons of equal weight, which made
    // the description and the tags the quietest part of the card. The chip is
    // also the honest answer to "why can I not launch this".
    jsxs('div', { className: 'ah-foot', children: [
      jsx('span', { className: 'ah-state' + (st.cls ? ' ' + st.cls : ''),
        title: st.title, children: st.text }),
      jsx('div', { className: 'ah-grow' }),
      // The one thing you came to do, and the one thing you reach for at the same
      // moment. Everything else is behind the menu, not gone from the card.
      jsx(Button, { size: 'micro', disabled: !!busy, onClick: () => pick(acts.primary),
        children: busyWord(acts.primary) }),
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy || acts.second.act === 'stop' && !!busy,
        onClick: () => pick(acts.second), children: busyWord(acts.second) }),
      jsx('button', {
        className: 'ah-kebab' + (menu ? ' ah-open' : ''),
        title: menu ? 'Close the menu' : 'Everything else you can do with this app',
        'aria-expanded': menu ? 'true' : 'false',
        onClick: () => setMenu(!menu),
        children: '\u22ef',
      }),
    ] }),
    menu
      ? jsx('div', { className: 'ah-menu', children: acts.more.map((a, i) =>
          jsx('button', {
            className: 'ah-mi' + (a.danger ? ' ah-danger' : '') + (a.sep ? ' ah-misep' : ''),
            key: 'm' + i, disabled: off(a), onClick: () => pick(a),
            children: busyWord(a),
          }, 'm' + i)) })
      : null,
  ] })
}

// The engine runs every app command through its own shell, which activates a
// bundled conda first. Without that toolchain the activation fails and anything
// chained after it never executes — an install clones its repo, builds nothing,
// and still looks like it worked. So this card is a real prerequisite, not a hint.
function EnvCard({ ctx }) {
  const [env, setEnv] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    try { setEnv(await ctx.rest('/environment', { timeoutMs: 60000 })) }
    catch (e) { setEnv({ ready: false, error: String(e) }) }
  }, [ctx])
  useEffect(() => { load() }, [load])

  // Polled while the tab is open, and quietly: a job that is still running is
  // the one thing here worth watching live, and a run started by the agent
  // appears the moment it begins rather than when it ends.
  useEffect(() => {
    const id = setInterval(() => load(true), 5000)
    return () => clearInterval(id)
  }, [load])

  const setup = useCallback(async () => {
    setBusy(true); setMsg('Downloading the toolchain — this can take a few minutes…')
    try {
      const r = await ctx.rest('/environment/provision', { method: 'POST', timeoutMs: 180000 })
      setMsg(r.ok ? 'Installing. Restart the engine when it finishes, then reopen this tab.'
                  : ('Setup failed: ' + (r.error || 'unknown error')))
      if (r.ok) setTimeout(load, 60000)
    } catch (e) { setMsg('Setup failed: ' + String(e)) } finally { setBusy(false) }
  }, [ctx, load])

  if (!env || env.ready) return null

  // Three different reasons land here and they need different answers. Asking
  // for setup while the engine is down — or while the backend predates the
  // route — reports a failure for something that was never going to work.
  const engineDown = env.engine_running === false
  const noRoute = !engineDown && !env.home
  if (engineDown || noRoute) {
    return jsxs('div', { className: 'ah-notice', children: [
      jsx('div', { className: 'ah-title', children: engineDown
        ? 'The engine is stopped' : 'Restart Hermes to finish this update' }),
      jsx('div', { className: 'ah-sub', children: engineDown
        ? 'Start the engine from the header first, then check again.'
        : 'This panel is newer than the backend it is talking to. Close and reopen Hermes so the plugin reloads, then check again.' }),
      jsxs('div', { className: 'ah-row', style: { marginTop: 8 }, children: [
        jsx('button', { className: 'ah-tabb', onClick: load, children: 'Re-check' }),
      ] }),
    ] })
  }

  return jsxs('div', { className: 'ah-notice ah-warn', children: [
    jsx('div', { className: 'ah-title', children: 'Finish setting up Athena' }),
    jsx('div', { className: 'ah-sub', children:
      'Apps run through a bundled toolchain that has not been installed yet. Until it is, apps will download but fail to build.' }),
    jsxs('div', { className: 'ah-row', style: { marginTop: 8 }, children: [
      jsx('button', { className: 'ah-tabb', disabled: busy, onClick: setup,
                      children: busy ? 'Setting up…' : 'Set up now' }),
      jsx('button', { className: 'ah-tabb', onClick: load, children: 'Re-check' }),
    ] }),
    msg ? jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children: msg }) : null,
  ] })
}

/* Does an app match what was typed into the search box?
   Pure, and top-level, so the test harness can call it with the same arguments
   the panel does. It matches the id as well as the name: names come from the
   store, ids come from a folder, and people search for both — "wan2gp" is the
   folder, "WAN2GP" is the name, and neither guess should come back empty.
   A description and its tags count too, because "video" is a thing you know
   about an app without remembering what it is called. */
function appMatches(app, needle) {
  const q = String(needle || '').trim().toLowerCase()
  if (!q) return true
  const hay = [app && app.name, app && app.id, app && app.description,
    ((app && app.tags) || []).join(' ')]
  return hay.some(v => String(v || '').toLowerCase().includes(q))
}

// Below this many apps a search box is furniture. Above it, the list is long
// enough that scrolling is the slow way to find one.
const SEARCH_FROM = 5

// The last answer to "is there anything to update to", kept outside the
// component so the bar does not blink out and back every time the tab is
// remounted, and stamped so it can go stale.
let lastUpdates = null
let lastUpdatesAt = 0
const UPDATE_TTL = 600000

function AppsTab({ ctx, st, refresh }) {
  const [apps, setApps] = useState(() => lastApps || [])
  const [q, setQ] = useState('')
  // Which slice of the list to show. Only ever about apps that are *here* — the
  // key scan reads folders on this machine, so there is nothing to filter a store
  // row by yet — and the count under the bar says how many were looked at, so an
  // empty result is not read as "nothing on this machine needs a key".
  const [only, setOnly] = useState('')
  const [upd, setUpd] = useState(() => lastUpdates)
  const [updBusy, setUpdBusy] = useState(false)
  const [updNote, setUpdNote] = useState('')
  // Only blank the tab when there is genuinely nothing to show. With a warm
  // memo this is false on mount, so the list is already on screen.
  const [loading, setLoading] = useState(lastApps === null)
  // Cards or rows. Read once from the same settings the Settings tab writes, so
  // switching it there redraws this tab rather than waiting for a reload.
  const [cfg] = useSettings(ctx)
  const row = (cfg && cfg.apps_layout) === 'rows'
  const [err, setErr] = useState('')
  const load = useCallback(async () => {
    try {
      const r = await ctx.rest('/apps', { timeoutMs: 60000 })
      lastApps = r.apps || []
      setApps(lastApps)
      setErr(r.error || '')
    } catch (e) { setErr(String(e)) } finally { setLoading(false) }
  }, [ctx])
  useEffect(() => {
    load()
    // An app boots for a while after Launch and then starts reporting a URL, so
    // the list is re-read on a slow tick rather than only when something here
    // changes — otherwise a card shows "not running" long after it is up.
    // 6s was inherited from a status readout. This is a list of installed
    // apps: it changes only when one is installed, launched or stopped, and
    // every one of those actions already reloads it directly. Polling that hard
    // cost the engine a request per second for a screen nobody was touching.
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load, st && st.ready])

  // Is there anything to update to? That question costs one network round trip
  // per installed app, so it is asked when the tab opens and when someone asks
  // again — never on the list's tick, which is a disk read and would turn into
  // seven requests to seven repositories every fifteen seconds. The answer is
  // kept for ten minutes so switching tabs does not ask again.
  const checkUpdates = useCallback(async (force) => {
    if (!force && lastUpdatesAt && Date.now() - lastUpdatesAt < UPDATE_TTL) {
      setUpd(lastUpdates)
      return
    }
    try {
      const r = await ctx.rest('/apps/updates?refresh=true', { timeoutMs: 180000 })
      // Only a real answer starts the ten minutes. A call that came back empty
      // has told us nothing, and treating "nothing" as an answer would keep the
      // bar quiet for the next ten minutes on the strength of a failed request.
      if (r && typeof r.any === 'boolean') {
        lastUpdatesAt = Date.now()
        lastUpdates = r.any
          ? { n: (r.updates || []).length, ids: r.updates || [], behind: r.behind || {} }
          : null
      }
    } catch { /* a repository that cannot be reached has nothing to say here */ }
    setUpd(lastUpdates)
  }, [ctx])
  useEffect(() => { checkUpdates(false) }, [checkUpdates])

  // Update every app that has moved on, in one go. Updating is the app's own
  // script — the same one its card runs — so this asks nothing of GitHub beyond
  // what the app itself does on a pull, and it needs no confirmation.
  const updateAll = async () => {
    setUpdBusy(true)
    setUpdNote('')
    // The steps are the whole story: which app, how far behind, whether it came
    // back as an Athena app. There is no single app to point the terminal at,
    // so it opens with none and shows the job alone — and it says what it is
    // waiting for while the request is still in flight, rather than pointing the
    // tab at a job that does not exist yet.
    activeJob.set({ app: null, id: null, label: 'Updating every app that moved' })
    tab.set('terminal')
    try {
      const r = await ctx.rest('/apps/update-all', { method: 'POST', timeoutMs: 60000 })
      if (!r || !r.ok) {
        const why = (r && (r.error || r.message)) || 'unknown'
        setUpdNote('Update failed: ' + why)
        notify(ctx, 'error', 'Update failed — ' + why)
        return
      }
      activeJob.set({ app: null, id: r.job, label: 'Updating every app that moved' })
      const out = await awaitJob(ctx, r.job, { onTick: () => {} })
      const done = ((out && out.result && out.result.updated) || []).length
      const failed = ((out && out.result && out.result.failed) || []).length
      if (out.ok) {
        setUpdNote(updateAllWords(done, failed))
        notify(ctx, 'info', `${done} app${done === 1 ? '' : 's'} updated`)
      } else {
        setUpdNote('Update failed: ' + (out.error || 'unknown'))
        notify(ctx, 'error', 'Update failed')
      }
    } catch (e) {
      setUpdNote(String(e))
    } finally {
      setUpdBusy(false)
      // Back to the list once there is nothing left to watch. Landing on the Terminal after
      // its job ended left the panel on a tab that is only drawn while a job exists: the tab
      // was gone from the bar and the screen was showing a tab nobody could see. The answer to
      // "did it update" is on the shelf, so that is where the note and the reader both go.
      activeJob.set(null)
      tab.set('apps')
      await checkUpdates(true)
      load()
    }
  }

  if (loading) return jsx('div', { className: 'ah-empty', children: 'Loading apps…' })
  // Only claim the engine is stopped once that is actually known. While the
  // first /status is still in flight, the list on screen came off disk and is
  // true whatever the engine is doing — gating on a status we do not have yet is
  // what flashed "The engine is stopped." across a perfectly good list.
  if (st && !st.ready) {
    return jsxs('div', { className: 'ah-empty', children: [
      jsx('div', { children: 'The engine is stopped.' }),
      jsx('div', { style: { marginTop: 6 }, children: 'Start it from the header to manage apps.' }),
    ] })
  }
  if (err) return jsx('div', { className: 'ah-empty ah-warn', children: err })
  if (!apps.length) {
    return jsxs('div', { children: [
      jsx(EnvCard, { ctx }),
      jsxs('div', { className: 'ah-empty', children: [
        jsx('div', { children: 'Nothing installed yet.' }),
        jsx('div', { style: { marginTop: 6 }, children: 'Open the Store tab to browse the catalogue, or paste a git repository there to make your own app.' }),
      ] }),
    ] })
  }
  const needle = q.trim()
  const shown = apps.filter(a => appMatches(a, needle) && keysMatch(a.keys, only))
  const scanned = apps.filter(a => a && a.keys && a.keys.scanned).length
  const needKey = apps.filter(a => keysMatch(a.keys, 'need')).length
  const runLocal = apps.filter(a => keysMatch(a.keys, 'local')).length
  const running = apps.filter(a => a && a.running)
  const open = (app) => openViewer(ctx, app, app.url || '')
  const stop = async (app) => {
    try {
      const r = await ctx.rest(`/apps/${encodeURIComponent(app.id)}/stop`,
        { method: 'POST', timeoutMs: 120000 })
      // The reload below is the answer for the *happy* path. A refusal needs words: the card
      // would otherwise come back exactly as it was, with no sign anything was tried.
      const why = actionFailure(`Stopping ${app.name || app.id}`, r)
      if (why) notify(ctx, 'error', why)
    } catch (e) { notify(ctx, 'error', `Stopping ${app.name || app.id}: ${e}`) }
    load()
  }
  return jsxs('div', { children: [
    jsx(EnvCard, { ctx }),
    // What is up right now, said once at the top. The grid below keeps every
    // app where it has always been — this is a short list of the ones holding
    // memory and a port, so stopping one does not mean finding its card first.
    running.length ? jsxs('div', { className: 'ah-runsec', children: [
      jsxs('div', { className: 'ah-runhead', children: [
        jsx('span', { children: running.length === 1 ? 'Running now' : `${running.length} running now` }),
      ] }),
      running.map(a => jsxs('div', { className: 'ah-runrow', key: `run-${a.id}`, children: [
        jsx('span', { className: 'ah-rundot' }),
        jsx('span', { className: 'ah-runname', children: a.name || a.id }),
        a.url ? jsx('span', { className: 'ah-runurl', children: a.url }) : null,
        jsxs('span', { className: 'ah-runbtns', children: [
          jsx('button', { className: 'ah-btn', type: 'button', onClick: () => open(a),
            children: 'Open' }),
          jsx('button', { className: 'ah-btn', type: 'button', onClick: () => stop(a),
            children: 'Stop' }),
        ] }),
      ] })),
    ] }) : null,
    // Anything to update to, said once for the whole shelf rather than app by
    // app. One app's card already carries its own update button; this is the
    // version of that question a person actually has.
    upd && upd.n ? jsxs('div', { className: 'ah-updbar', children: [
      jsx('span', { className: 'ah-updtext', children: upd.n === 1
        ? `1 app has an update — ${String(upd.ids[0] || '').replace(/\.git$/, '')}`
        : `${upd.n} apps have updates` }),
      updNote ? jsx('span', { className: 'ah-updnote', children: updNote }) : null,
      jsx('div', { className: 'ah-grow' }),
      jsx('button', {
        className: 'ah-btn', type: 'button', disabled: updBusy,
        onClick: updateAll, children: updBusy ? 'Updating…' : 'Update all',
      }),
      jsx('button', {
        className: 'ah-btn', type: 'button', disabled: updBusy,
        onClick: () => checkUpdates(true), children: 'Recheck',
      }),
    ] }) : (updNote ? jsx('div', { className: 'ah-updbar', children: [
      jsx('span', { className: 'ah-updnote', children: updNote }),
      jsx('div', { className: 'ah-grow' }),
      jsx('button', {
        className: 'ah-btn', type: 'button', disabled: updBusy,
        onClick: () => checkUpdates(true), children: 'Recheck',
      }),
    ] }) : null),
    // The filter row. Shown even for seven apps: "what here wants something from
    // me" is worth being able to ask of a short list, and the sentence beside the
    // chips is what keeps an empty answer honest — it says how much of the list
    // has actually been read, so "Needs a key 0" cannot pass for a clean machine.
    jsxs('div', { className: 'ah-filtrow', children: [
      jsx('button', { className: 'ah-chip' + (only === '' ? ' on' : ''), type: 'button',
        onClick: () => setOnly(''), children: `All ${apps.length}` }),
      jsx('button', { className: 'ah-chip' + (only === 'need' ? ' on' : ''), type: 'button',
        onClick: () => setOnly('need'),
        title: 'Apps whose scripts name a key Hermes cannot answer, or a cloud endpoint. Read from the folders on this machine.',
        children: `Needs a key ${needKey}` }),
      jsx('button', { className: 'ah-chip' + (only === 'local' ? ' on' : ''), type: 'button',
        onClick: () => setOnly('local'),
        title: 'Apps whose scripts name no key and no cloud endpoint, in a complete read of the folder.',
        children: `Runs here ${runLocal}` }),
      jsx('span', { className: 'ah-sub', children: scanned === apps.length
        ? `all ${apps.length} read`
        : `${scanned} of ${apps.length} read — the rest have not been looked at` }),
    ] }),
    apps.length >= SEARCH_FROM ? jsxs('div', { className: 'ah-search', children: [
      jsx('input', {
        className: 'ah-searchin', type: 'search', value: q,
        placeholder: 'Find an app by name, id or tag…',
        'aria-label': 'Find an app',
        onChange: (e) => setQ(e.target.value),
      }),
      jsx('span', { className: 'ah-sub', children: needle
        ? `${shown.length} of ${apps.length} apps`
        : `${apps.length} apps${running.length ? `, ${running.length} running` : ''}` }),
    ] }) : null,
    shown.length ? jsx('div', { className: 'ah-grid' + (row ? ' ah-rows' : ''), children: shown.map(a =>
      jsx(AppCard, { ctx, app: a, onChange: load, row }, a.id)) })
      : jsx('div', { className: 'ah-empty', children: only
        ? (scanned === 0
          ? 'Nothing here has been read yet — press “Read its scripts” on a card, then come back.'
          : (only === 'need'
            ? 'Of the apps that were read, none needs a key.'
            : 'Of the apps that were read, none runs entirely on this box.'))
        : `No app matches “${needle}”.` }),
  ] })
}

/* ---------------------------------------------------------------- store */

/* A terminal that follows its own output — but only while you are not reading
   it. Scrolling up stops the follow until you come back to the bottom, so a
   build that prints for ten minutes never yanks the line you are looking at out
   from under you. */
function AutoTerm({ text, className = 'ah-term', placeholder }) {
  const box = useRef(null)
  const follow = useRef(true)
  const body = typeof text === 'string' ? text : (text == null ? '' : String(text))
  useEffect(() => {
    const el = box.current
    if (el && follow.current) el.scrollTop = el.scrollHeight
  }, [body])
  return jsx('pre', {
    className: className,
    ref: box,
    onScroll: (e) => {
      const el = e.currentTarget
      follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
    },
    children: body || placeholder || '',
  })
}

/* Installs outlive the card that started them. Switching tabs unmounts the whole
   store, but the clone and the build carry on in the backend: the record is kept
   here, at module scope, and the card reads it back on mount. Coming back to the
   store therefore shows the install exactly where it got to, rather than a card
   that has forgotten it ever began.
   The poll is started from this module too — `awaitJob` runs in its own async
   chain, not on the component's lifetime, so it keeps landing output while the
   user is somewhere else entirely. */
const INSTALLS = new Map()
const installListeners = new Set()

function installRead(key) {
  return INSTALLS.get(key) || null
}

function installWrite(key, patch) {
  const next = Object.assign({}, INSTALLS.get(key) || {}, patch)
  INSTALLS.set(key, next)
  // Bounded: the store gets browsed for hours and every card keeps its last
  // install. Only settled records are dropped, oldest first, never this one.
  if (INSTALLS.size > 60) {
    for (const [k, v] of INSTALLS) {
      if (INSTALLS.size <= 40) break
      if (k !== key && (v.state === 'installed' || v.state === 'failed')) INSTALLS.delete(k)
    }
  }
  for (const fn of installListeners) {
    try { fn(key, next) } catch { /* a listener must never break the writer */ }
  }
  return next
}

function useInstall(key) {
  const [rec, setRec] = useState(() => installRead(key))
  useEffect(() => {
    const fn = (k, next) => { if (k === key) setRec(next) }
    installListeners.add(fn)
    // Re-read on mount: the record may have moved on while this card was gone.
    setRec(installRead(key))
    return () => { installListeners.delete(fn) }
  }, [key])
  return rec
}

/* The whole install, from the clone to the build, written to the registry at
   every step so any render of the card — before, during or long after — shows
   the truth. Returns whether it finished, for the caller's refresh. */
async function startInstall(ctx, key, slug, label) {
  installWrite(key, { state: 'installing', note: '', text: '', progress: null,
                      started: Date.now() })
  try {
    const r = await ctx.rest('/catalog/install',
      { method: 'POST', timeoutMs: 180000, body: { url: slug } })
    if (!r || !r.ok) {
      installWrite(key, { state: 'failed', note: (r && r.error) || 'install failed' })
      notify(ctx, 'error', `Install failed: ${(r && r.error) || 'unknown'}`)
      return false
    }
    if (!r.job) {
      // A backend that answers inline has nothing to watch.
      installWrite(key, { state: 'installed' })
      notify(ctx, 'info', `Installed ${label || slug}`)
      return true
    }
    installWrite(key, { state: 'building', job: r.job })
    const out = await awaitJob(ctx, r.job, {
      onTick: (j) => installWrite(key, {
        text: (j && j.tail) || '', progress: (j && j.progress) || null,
      }),
    })
    const tail = (out.job && out.job.tail) || ''
    if (!out.ok) {
      installWrite(key, { state: 'failed', note: out.error || 'install failed',
                          text: tail, progress: (out.job && out.job.progress) || null })
      notify(ctx, 'error', `Install failed: ${out.error || 'unknown'}`)
      return false
    }
    installWrite(key, { state: 'installed', text: tail,
      progress: (out.job && out.job.progress) || null,
      note: (out.result && out.result.built === false)
        ? 'The script finished, but the app did not change its default action — read the output before trusting it.'
        : '' })
    notify(ctx, 'info', `Installed ${label || slug}`)
    return true
  } catch (e) {
    installWrite(key, { state: 'failed', note: String(e) })
    notify(ctx, 'error', `Install failed: ${e}`)
    return false
  }
}

/* Whether anyone has this working on a machine shaped like this one.
   The catalogue declares no VRAM and no disk figure — never has — so the only
   honest rig-fit facts a store row carries are the *check-ins*: has somebody ever
   reported this app from Windows, from an NVIDIA card. That is evidence, not a
   requirement, and the wording keeps the two apart: "runs on Windows" is something
   people did, not something the app demands. A row nobody has checked in from is
   an absence of evidence, said as an absence. */
function rigBadge(app) {
  const p = (app && app.platforms) || {}
  const g = (app && app.gpus) || {}
  const n = (app && app.checkins) || 0
  if (!n || (!p.windows && !p.mac && !p.linux && !g.nvidia && !g.amd && !g.apple)) {
    return { cls: '', text: 'nobody has run it',
      title: 'No check-ins yet. That is an absence of evidence, not a verdict on the app.' }
  }
  if (p.windows && g.nvidia) {
    return { cls: 'run', text: 'runs on Windows',
      title: `${n} check-in${n === 1 ? '' : 's'} here, including Windows on NVIDIA — the same shape as this box. Says nothing about speed.` }
  }
  if (p.windows) {
    return { cls: '', text: 'Windows, no NVIDIA',
      title: `${n} check-in${n === 1 ? '' : 's'} from Windows, but none from an NVIDIA card — it may be CPU-only, or just new.` }
  }
  return { cls: 'need', text: 'no Windows check-in',
    title: `${n} check-in${n === 1 ? '' : 's'}, none of them from Windows. It may still work — nobody has said so yet.` }
}

/* The filter pair to `rigBadge`: whatever the badge claims, this keeps — the two
   cannot disagree, and the count under the bar says it is counting the loaded
   page, because the store's ranking cannot be asked for this across 6,400 apps. */
function rigMatch(app, on) {
  if (!on) return true
  const p = (app && app.platforms) || {}
  const g = (app && app.gpus) || {}
  return !!(p.windows && g.nvidia)
}

function StoreCard({ ctx, app, onInstalled }) {
  // The install itself lives in the module-level registry, not in this
  // component, so leaving the store and coming back finds it still under way.
  const inst = useInstall(app.slug)
  const [busy, setBusy] = useState('')
  const [detail, setDetail] = useState(null)
  const [openDetail, setOpenDetail] = useState(false)
  const [localNote, setLocalNote] = useState('')

  const term = (inst && inst.text) || ''
  const prog = (inst && inst.progress) || null
  const note = (inst && inst.note) || localNote
  const state = (inst && inst.state) || busy

  // An install is a clone followed by the app building itself, which takes
  // minutes. This used to report success the instant the request returned, so an
  // app that had only been cloned was announced as installed and then did
  // nothing when launched. It now waits on the actual build and shows the output
  // as it arrives — and the waiting happens in `startInstall`, outside this
  // component's lifetime, so a tab switch cannot abandon it.
  const install = () => {
    setLocalNote('')
    startInstall(ctx, app.slug, app.slug, app.name || app.slug)
      .then((done) => { if (done) onInstalled && onInstalled() })
      .catch(() => { /* startInstall reports its own failures */ })
  }

  // Details used to fetch this and render nothing from it: the two fields it
  // read were already on the card, so the button appeared dead. It now opens a
  // real panel with everything the store knows.
  const details = async () => {
    if (openDetail) { setOpenDetail(false); return }
    setBusy('detail')
    setLocalNote('')
    try {
      const d = await ctx.rest(`/catalog/detail?ref=${encodeURIComponent(app.slug)}`,
        { timeoutMs: 30000 })
      setDetail(d || {})
      setOpenDetail(true)
    } catch (e) {
      setLocalNote(String(e))
      notify(ctx, 'error', `Details: ${e}`)
    } finally { setBusy('') }
  }

  const repo = (detail && (detail.repo || detail.repoUrl || detail.git)) || app.repo || ''
  const desc = (detail && detail.description) || app.description || ''
  const allTags = (detail && detail.tags && detail.tags.length ? detail.tags : app.tags) || []
  return jsxs('div', { className: 'ah-card',
    // Athena installs by slug (`install_with`) but the agent may have reached the app
    // by its display name, so the card answers to both.
    'data-athena-spot': 'store:' + app.slug + (app.name ? ' store:' + app.name : ''), children: [
    jsxs('div', { className: 'ah-cartop', children: [
      jsx(Icon, { src: app.icon, name: app.name || app.slug }),
      jsxs('div', { style: { minWidth: 0 }, children: [
        jsxs('h4', { children: [
          jsx('span', { children: app.name || app.slug }),
          app.version ? jsx('span', { className: 'ah-badge', children: 'v' + app.version }) : null,
          // Where this app came from. The card used to carry a second badge
          // claiming an agent could already drive the app; that claim is out of
          // the store until it is true of every app, not just the converted ones.
          app.store === 'athena'
            ? jsx('span', { className: 'ah-badge own', children: 'Athena Store' }) : null,
          (() => { const b = rigBadge(app)
            return jsx('span', { className: 'ah-badge' + (b.cls ? ' ' + b.cls : ''),
              title: b.title, children: b.text }) })(),
        ] }),
        repo ? jsx('div', { className: 'ah-repo', children: repo }) : null,
      ] }),
    ] }),
    jsx('p', { children: desc || 'No description in the store.' }),
    (app.tags && app.tags.length)
      ? jsx('div', { className: 'ah-row', children: app.tags.slice(0, 4).map((t, i) =>
          jsx('span', { className: 'ah-badge', children: '#' + t }, 't' + i)) })
      : null,
    note ? jsx('div', { className: 'ah-sub ah-warn', children: note }) : null,
    prog ? jsx(JobProgress, { progress: prog }) : null,
    // Kept mounted through the whole install, not only when there is output
    // yet: the box is where the first line appears, and a box that arrives
    // with it cannot show that it is waiting.
    (state === 'installing' || state === 'building' || term)
      ? jsx(AutoTerm, { text: term,
          placeholder: state === 'installing'
            ? 'Starting the clone…'
            : 'Building. The app prints here as it installs…' })
      : null,
    jsxs('div', { className: 'ah-row', children: [
      state === 'installed'
        ? jsx('span', { className: 'ah-badge run', children: 'installed' })
        : jsx(Button, { size: 'micro', disabled: state === 'installing' || state === 'building',
            onClick: install,
            children: state === 'installing' ? 'Installing…'
              : (state === 'building' ? 'Building…'
              : (state === 'failed' ? 'Retry' : 'Install')) }),
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: state === 'detail',
        onClick: details,
        children: state === 'detail' ? '…' : (openDetail ? 'Hide' : 'Details') }),
    ] }),
    openDetail ? jsx('div', { className: 'ah-detail', children: (() => {
      const d = detail || {}
      const rows = [
        ['Repository', repo || '—'],
        ['Version', d.version || app.version || '—'],
        ['Author', d.owner || '—'],
        ['Last updated', d.updated ? String(d.updated).slice(0, 10) : '—'],
        ['Runs', d.checkins ? `${d.checkins} installs reported` : '—'],
        ['Tags', allTags.length ? allTags.map(t => '#' + t).join('  ') : '—'],
      ]
      const plats = Object.keys(d.platforms || {}).filter(k => (d.platforms || {})[k])
      return [
        jsxs('dl', { className: 'ah-dl', children: rows.flatMap(([k, v], i) => [
          jsx('dt', { children: k }, 'k' + i),
          jsx('dd', { children: v }, 'v' + i),
        ]) }),
        plats.length
          ? jsx('div', { className: 'ah-sub', children: 'Detected platforms: ' + plats.join(', ') })
          : null,
        repo ? jsx('div', { className: 'ah-row', children: jsx(Button, {
            size: 'micro', variant: 'ghost',
            onClick: () => {
              const url = repo.startsWith('http') ? repo : `https://github.com/${repo}`
              if (ctx.host && ctx.host.openExternal) ctx.host.openExternal(url)
              else window.open(url, '_blank')
            },
            children: 'Open repository',
          }) }) : null,
        jsx('div', { className: 'ah-sub',
          children: 'Installing clones the repository, then runs the app\u2019s own '
                  + 'install action and streams its output right here on the card.' }),
      ]
    })() }) : null,
  ] })
}
function StoreTab({ ctx, st, refresh }) {
  const [q, setQ] = useState('')
  const [active, setActive] = useState('')
  const [apps, setApps] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  // Athena's own catalogue is published but empty while its requirements are
  // being worked out. That is not a failure to report in red: the panel says
  // it is coming soon, and the public catalogue underneath still browses.
  const [athenaSoon, setAthenaSoon] = useState(false)
  const [athenaWhy, setAthenaWhy] = useState('')
  const [repoUrl, setRepoUrl] = useState('')
  const [repoMsg, setRepoMsg] = useState('')
  const [showRepo, setShowRepo] = useState(false)
  // Building an app from a repository: the panel scaffolds, then hands the job
  // to the agent through the composer — Athena has no model of its own.
  const [building, setBuilding] = useState(false)
  const [buildNote, setBuildNote] = useState('')
  const [noLauncher, setNoLauncher] = useState(false)
  const [promptText, setPromptText] = useState('')
  const [tag, setTag] = useState('')
  const [sort, setSort] = useState('trending')
  const [tagList, setTagList] = useState([])
  const [showTags, setShowTags] = useState(false)
  // Whether to show only what somebody has run on a box like this one.
  // A chip rather than a mode: "it has been run on Windows + NVIDIA" is a
  // different question from anything else the shelf can be narrowed by, and
  // someone wants to see it alone.
  const [rigOnly, setRigOnly] = useState(false)

  const searchSeq = useRef(0)
  const searchTimer = useRef(null)
  useEffect(() => () => { if (searchTimer.current) clearTimeout(searchTimer.current) }, [])

  const fetchPage = useCallback(async (query, off, append, useTag, useSort) => {
    const mine = searchSeq.current
    setLoading(true)
    setErr('')
    try {
      const r = await ctx.rest(
        `/catalog?q=${encodeURIComponent(query || '')}&offset=${off}&limit=${PAGE}` +
        `&tag=${encodeURIComponent(useTag || '')}&sort=${encodeURIComponent(useSort || 'trending')}`,
        { timeoutMs: 90000 })
      if (mine !== searchSeq.current) return   // a newer search is already out
      const list = r.apps || []
      setApps((prev) => (append ? [...prev, ...list] : list))
      if (typeof r.total === 'number') setTotal(r.total)
      // Athena's own catalogue: empty is a published state, not an error. The
      // backend reports the two separately (`athena_empty` vs `athena_error`).
      setAthenaSoon(r.athena_empty === true)
      setAthenaWhy(r.athena_error || '')
      setOffset(off + list.length)
      // The store is the live source; the cached crawl only steps in when it
      // cannot be reached, and saying so beats silently showing stale results.
      if (r.degraded) setErr('Store unreachable — showing the cached catalogue.')
      else if (r.error) setErr(r.error)
      else setErr('')
    } catch (e) { if (mine === searchSeq.current) setErr(String(e)) }
    finally { if (mine === searchSeq.current) setLoading(false) }
  }, [ctx])

  useEffect(() => {
    ctx.rest('/catalog/tags?limit=200', { timeoutMs: 60000 })
      .then((r) => setTagList(r.tags || [])).catch(() => {})
  }, [ctx])

  useEffect(() => { fetchPage('', 0, false, '', 'trending') }, [fetchPage])

  /* One search per pause, not one per keystroke, and only the newest one may land.
     Every character used to fire its own `/catalog` request with a 90-second timeout: typing
     "wan2gp" sent seven searches at a store that answers in seconds, and their replies came
     back in whatever order the store finished them, so an early one could land after a later
     one and put the wrong list under the box. The sequence number is what makes an answer
     belong to a question: a reply from a keystroke that has been superseded is dropped
     instead of drawn. */
  const onQuery = (value) => {
    setQ(value)
    setActive(value)
    const mine = (searchSeq.current += 1)
    if (searchTimer.current) clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => {
      if (mine === searchSeq.current) fetchPage(value, 0, false, tag, sort)
    }, SEARCH_SETTLE_MS)
  }

  // A tag click or a sort change is a newer question than anything still being typed, so a
  // pending keystroke is cancelled rather than allowed to land after it.
  const supersede = () => {
    const mine = (searchSeq.current += 1)
    if (searchTimer.current) clearTimeout(searchTimer.current)
    return mine
  }

  const applyTag = (value) => {
    setTag(value)
    supersede()
    fetchPage(q, 0, false, value, sort)
  }

  const applySort = (value) => {
    setSort(value)
    supersede()
    fetchPage(q, 0, false, tag, value)
  }

  const installRepo = async () => {
    const target = repoUrl.trim()
    if (!target) { setRepoMsg('Paste a git repository URL first.'); return }
    setRepoMsg(`Installing ${target}… this can take a while for large apps.`)
    try {
      const r = await ctx.rest('/catalog/install',
        { method: 'POST', timeoutMs: 3600000, body: { url: target } })
      if (r && r.ok && r.job) {
        // The route answers with a job, and it is a job for a reason: an install clones a
        // repository and *then* builds it. Reading `ok` as "installed" is a sentence about
        // work that has not happened yet — the clone landed, the build had not started — so
        // the job is watched and the sentence comes from what it ended up doing.
        if (r.app) termApp.set(r.app)
        activeJob.set({ app: r.app || null, id: r.job, label: `Installing ${target}` })
        tab.set('terminal')
        const out = await awaitJob(ctx, r.job, { onTick: (j) => {
          const last = String((j && j.tail) || '').trim().split('\n').filter(Boolean).pop() || ''
          setRepoMsg(`Installing ${target}…` + (last ? ' ' + last : ''))
        } })
        if (!out.ok) {
          // Stay where the output is: the reason it failed is in there, and a reader sent
          // back to the card would be sent away from the only thing that explains it.
          setRepoMsg(`Failed: ${out.error || 'unknown'}`)
          return
        }
        activeJob.set(null)
        termApp.set(null)
        tab.set('apps')
        setRepoMsg('Installed and built. See the Apps tab.')
        setRepoUrl('')
        await refresh()
      } else if (r && r.ok) {
        setRepoMsg('Installed. See the Apps tab.')
        setRepoUrl('')
        await refresh()
      } else {
        const why = (r && r.error) || 'unknown'
        setRepoMsg(`Failed: ${why}`)
        // A repository with no launcher is not an error to end on — it is the
        // case the builder exists for. Offer it rather than making the user
        // work out why the install refused.
        if (/no athena\.js|not an app|not a runnable app|no launcher/i.test(why)) {
          setNoLauncher(true)
          setBuildNote('This repository has no launcher of its own. Tala can read it and write one.')
        }
      }
    } catch (e) { setRepoMsg('Failed: ' + e) }
  }

  const buildWithAgent = async () => {
    const target = repoUrl.trim()
    if (!target) { setBuildNote('Paste a git repository URL first.'); return }
    setBuilding(true)
    setPromptText('')
    setBuildNote('Reading the repository…')
    try {
      const r = await ctx.rest('/catalog/build-prompt',
        { method: 'POST', timeoutMs: 600000, body: { url: target } })
      if (!r || !r.ok) { setBuildNote('Could not start: ' + ((r && r.error) || 'unknown')); return }
      const h = ctx.host
      const sent = (h && h.composer && h.composer.submit) ? h.composer.submit(null, r.prompt) : false
      if (sent) {
        setBuildNote(`Tala is building “${r.app}” in this chat. Watch the Apps tab — it will be there when the launcher is written and tested.`)
        setNoLauncher(false)
      } else {
        // Older host without the composer door: hand the user the prompt rather
        // than dropping it on the floor.
        setPromptText(r.prompt)
        setBuildNote('Paste this into the chat to start the build:')
      }
    } catch (e) { setBuildNote('Failed: ' + e) } finally { setBuilding(false) }
  }

  // Which of the loaded records have been checked in from a box like this one —
  // read from the store's own flags, so this narrows the page on screen rather
  // than claiming to count the whole catalogue, which the store's ranking gives
  // no way to ask.
  const rigCount = apps.filter((a) => rigMatch(a, true)).length
  const shown = apps.filter((a) => rigMatch(a, rigOnly))

  return jsxs('div', { children: [
    jsxs('div', { className: 'ah-row', style: { marginBottom: 4 }, children: [
      jsx('div', { style: { flex: '1 1 220px' }, children: jsx(Field, {
        value: q, placeholder: 'Search the app store…', onChange: onQuery, onSubmit: () => onQuery(q),
      }) }),
      jsx(Button, { size: 'micro', variant: 'ghost',
        onClick: () => setShowRepo(!showRepo),
        children: showRepo ? 'Hide repo install' : 'Install from repo' }),
    ] }),
    jsxs('div', { className: 'ah-row', style: { marginBottom: 4 }, children: [
      jsx('span', { className: 'ah-sub', children: 'Sort' }),
      ...[['trending', 'Trending'], ['popular', 'Most used'], ['latest', 'Recent'], ['name', 'A–Z']]
        .map(([k, label]) => jsx('button', {
          className: 'ah-tabb' + (sort === k ? ' active' : ''),
          style: { padding: '3px 8px' },
          onClick: () => applySort(k), children: label,
        }, k)),
      // The rig chip. It narrows to what somebody has actually checked in from a
      // Windows box with an NVIDIA card — the shape of this machine — and the
      // count line below says how many of the loaded page that is, because the
      // store's ranking cannot be asked this question across 6,400 apps.
      jsx('button', {
        className: 'ah-tabb' + (rigOnly ? ' active' : ''), style: { padding: '3px 8px' },
        onClick: () => setRigOnly(!rigOnly),
        title: 'Apps somebody has checked in from Windows on an NVIDIA card. '
          + 'Evidence of it working there, not a promise about speed.',
        children: rigOnly ? 'Windows + NVIDIA ✓' : 'Windows + NVIDIA',
      }),
      jsx('div', { className: 'ah-grow' }),
      jsx(Button, { size: 'micro', variant: 'ghost',
        onClick: () => setShowTags(!showTags),
        children: showTags ? 'Hide filters' : (tag ? `#${tag}` : 'Filter by tag') }),
    ] }),

    // The tags and their counts are the store's own filter list, read from the
    // store rather than tallied locally — the local count undercounts because
    // each record only carries its leading tags.
    showTags
      ? jsxs('div', { className: 'ah-notice', children: [
          jsxs('div', { className: 'ah-row', children: [
            jsx('button', { className: 'ah-tabb' + (tag ? '' : ' active'),
              style: { padding: '3px 8px' }, onClick: () => applyTag(''),
              children: 'All' }),
            ...tagList.slice(0, 60).map((t) => jsx('button', {
              className: 'ah-tabb' + (tag === t.tag ? ' active' : ''),
              style: { padding: '3px 8px' },
              onClick: () => applyTag(tag === t.tag ? '' : t.tag),
              children: `#${t.tag} (${t.count})`,
            }, t.tag)),
          ] }),
          tagList.length > 60
            ? jsx('div', { className: 'ah-sub', style: { marginTop: 6 },
                children: `${tagList.length - 60} more tags — or type a tag name in the search box.` })
            : null,
        ] })
      : null,

    jsx('div', { className: 'ah-sub', style: { marginBottom: 8 }, children: active
      ? `${shown.length} result${shown.length === 1 ? '' : 's'} for “${active}”`
      : (tag ? `#${tag} · ${total.toLocaleString()} apps`
             : `Browsing ${total.toLocaleString()} apps`)
        + (rigOnly ? ` · ${rigCount} of the ${apps.length} loaded run on Windows + NVIDIA` : '') }),

    showRepo
      ? jsxs('div', { className: 'ah-notice', children: [
          jsx('div', { className: 'ah-sub', style: { marginBottom: 6 }, children:
            'Turn any git repository into an Athena app. Credentials are already shared from Hermes, so private repositories and gated models work with no extra sign-in.' }),
          jsxs('div', { className: 'ah-row', children: [
            jsx('div', { style: { flex: '1 1 200px' }, children: jsx(Field, {
              value: repoUrl, placeholder: 'https://github.com/owner/my-app',
              onChange: setRepoUrl, onSubmit: installRepo,
            }) }),
            jsx(Button, { size: 'micro', onClick: installRepo, children: 'Install' }),
            jsx(Button, { size: 'micro', variant: 'ghost', disabled: building,
              onClick: buildWithAgent,
              children: building ? 'Building…' : 'Build with Tala' }),
          ] }),
          repoMsg ? jsx('div', { className: 'ah-sub', style: { marginTop: 8 }, children: repoMsg }) : null,
          noLauncher
            ? jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children:
                'No launcher in that repository. “Build with Tala” reads the repo and writes one — Athena ships the guide it follows.' })
            : null,
          buildNote ? jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children: buildNote }) : null,
          promptText
            ? jsx('textarea', {
                className: 'ah-mono', readOnly: true, value: promptText,
                style: { width: '100%', height: 120, marginTop: 6, resize: 'vertical' },
                onFocus: (e) => e.target.select(),
              })
            : null,
        ] })
      : null,

    err ? jsx('div', { className: 'ah-warn', style: { marginBottom: 8 }, children: err }) : null,

    // Athena's own catalogue is a placeholder for launch: the repository exists
    // and its index is empty. Saying that once, calmly, is the whole empty state
    // — no red banner, no spinner, no traceback. `athena_error` is kept apart
    // from `athena_empty` so the sentence can be true about which happened.
    (athenaSoon && !active)
      ? jsx('div', { className: 'ah-empty', style: { marginBottom: 10 }, children:
          'Athena’s own catalogue is coming soon — '
          + (athenaWhy
              ? 'it could not be read just now, which is expected while it is being prepared.'
              : 'nothing has been published into it yet.') })
      : null,

    // Grid renders what the filter left. When nothing is left, the reason is the
    // store's own pace of publishing, not an error, so it says which it is.
    jsx('div', { className: 'ah-grid', children: shown.map((a, i) =>
      jsx(StoreCard, { ctx, app: a, onInstalled: refresh }, a.slug + ':' + i)) }),

    (rigOnly && apps.length && !shown.length)
      ? jsx('div', { className: 'ah-empty', children:
          `None of the ${apps.length} apps loaded has been checked in from Windows on an `
          + 'NVIDIA card. That is not a verdict — nobody has said either way.' })
      : null,

    loading && !apps.length ? jsx('div', { className: 'ah-empty', children: 'Loading…' }) : null,

    (!active && apps.length && apps.length < total)
      ? jsx('div', { style: { marginTop: 10, textAlign: 'center' }, children:
          jsx(Button, { size: 'small', disabled: loading,
            onClick: () => fetchPage('', offset, true, tag, sort),
            children: loading ? 'Loading…' : 'Load more' }) })
      : null,
  ] })
}

/* ------------------------------------------------------------ terminal */

/* The tab bar. The Terminal tab is not standing furniture: it is in the bar only
   while an app's terminal is open, and only the app's own button opens one.
   Spliced before the last tab so it sits among the app views it belongs to and
   never pushes Settings around. Kept pure so the bar can be asserted without
   mounting the panel. */
/* ------------------------------------------------------------- machines */

/* A machine is a real OS Athena can start, stop and hand a command to, and there
   is exactly one reason it exists: this computer cannot answer every question
   about itself. An app that only installs on Linux, a build that wants Arch,
   "does this work on Windows" without guessing — each of those is a question you
   answer by having the OS rather than by reasoning about it.

   Claimed, never shared. A machine Athena makes is its own distro, so a command
   run inside it cannot reach the folders you are working in. Attaching to a
   distro you already have is offered and never assumed, because it is the single
   setting where an agent shell would be standing in your workspace — the one
   place this whole tab could do damage, so it takes a decision rather than a
   default.

   Setup is a question, not a service. Nothing is installed by opening this tab:
   a missing WSL or QEMU arrives as a card carrying the exact command and what it
   will cost, and the button on it is the answer. */

/* The one chip on a machine's card. Pure, like appStateWords: it words a state
   the engine already decided, so drawing a card never has to ask WSL anything. */
// The pairs alone, for a caller with nothing to say about the rest. The reading lives in
// `portPairs`, above, so this and the settings form cannot drift into two different ideas of
// what a port pair is.
function parsePorts(text) {
  return portPairs(text).pairs
}

function machinePorts(m) {
  const out = []
  if (!m) return out
  const ssh = m.ssh && m.ssh.port
  if (ssh) out.push(`${ssh}→22`)
  ;((m && m.ports) || []).forEach((p) => {
    if (p && p.host && p.guest) out.push(`${p.host}→${p.guest}`)
  })
  return out
}

/* The image folder, and the menu drawn from it. Athena's own `isos` folder is where
   files can go on day one; pointing it somewhere else is the user's decision, so the
   panel shows the folder next to the menu instead of burying it in a setting. */
function isoSize(mb) {
  const n = Number(mb) || 0
  if (n >= 1024) return `${(n / 1024).toFixed(1)} GB`
  if (n >= 1) return `${Math.round(n)} MB`
  return `${Math.round(n * 1024)} KB`
}

/* One line for the folder: where it is and what is in it, or that it is not there.
   Never "0 images" for a folder that does not exist — those are two different facts. */
function isoWords(scan) {
  const s = scan || {}
  if (!s.dir) return 'No image folder yet'
  if (!s.exists) return `${s.dir} \u2014 not there yet`
  const n = (s.isos || []).length
  if (!n) return `${s.dir} \u2014 no images yet`
  // Folders in the same sentence, because it is the sentence that tells somebody with
  // windows/, linux/ and mac/ that Athena read *inside* those folders.
  const f = Number(s.folders) || 0
  return `${s.dir} \u2014 ${n} ${n === 1 ? 'image' : 'images'}${f > 1 ? ` in ${f} folders` : ''}`
}

/* A file named in the menu. The kind is the one the *name* suggests, labelled as
   what it is, so nobody has to remember which extension means what. */
function isoLabel(item) {
  const i = item || {}
  const kind = i.kind === 'iso' ? 'installer'
    : i.kind === 'disk' ? 'disk image'
      : i.kind === 'rootfs' ? 'rootfs tarball' : 'kind not obvious from the name'
  return `${i.name || 'file'} \u2014 ${kind}, ${isoSize(i.size_mb)}`
}

function isoOptions(scan) {
  const out = [{ value: '', label: 'choose a file from the folder\u2026' }]
  ;((scan || {}).isos || []).forEach((i) => {
    if (!i || !i.path) return
    // The subfolder belongs in the label: two files called `install.iso` in
    // `windows/` and `linux/` are two different files, and the menu must say so.
    const rel = String(i.rel || '')
    out.push({ value: String(i.path), label: rel ? `${rel}/${isoLabel(i)}` : isoLabel(i),
      kind: i.kind || '' })
  })
  out.push({ value: '__other__', label: 'somewhere else on this computer\u2026' })
  return out
}

/* Images grouped by the folder they live in, the top folder first. A folder organised
   as windows/ linux/ mac/ should read as three short lists rather than one long one —
   and the grouping is decided here, in a function, so the panel and the tests agree on
   what a folder looks like. Each group carries its images in the order the server gave
   them (newest first). */
function isoGroups(scan) {
  const by = {}
  ;((scan || {}).isos || []).forEach((i) => {
    if (!i || !i.path) return
    const rel = String(i.rel || '')
    if (!by[rel]) by[rel] = []
    by[rel].push(i)
  })
  return Object.keys(by)
    .sort((a, b) => (a === '' ? -1 : b === '' ? 1 : a.localeCompare(b)))
    .map((rel) => ({ rel, label: rel || 'in this folder', items: by[rel] }))
}

/* Which groups are showing. A folder somebody has been filling for years is a wall of
   filenames, so a subfolder starts collapsed and reads as one line you can open; the
   top folder starts open, because a loose ISO sitting in it is the common case and
   hiding it behind a click would be a worse default than showing it.
   `openMap` is keyed by the folder's relative path, and a folder that is *in* the map
   is believed over the default — including `false`, which is why this asks whether the
   key exists rather than whether its value is true. */
function isoGroupsShown(scan, openMap) {
  const open = openMap || {}
  return isoGroups(scan).map((g) => ({
    rel: g.rel, label: g.label, items: g.items, count: g.items.length,
    open: Object.prototype.hasOwnProperty.call(open, g.rel) ? !!open[g.rel] : g.rel === '',
  }))
}

/* Every folder at once, open or shut. One function because both answers build the same
   shape, and because "expand all" that disagreed with the per-folder toggle would be
   two rules for one thing. */
function groupMap(scan, open) {
  const out = {}
  isoGroups(scan).forEach((g) => { out[g.rel] = !!open })
  return out
}

/* One line for the folder browser: where you are standing and what is in there. An
   unreadable folder says so rather than looking empty. */
function folderWords(b) {
  const d = b || {}
  if (!d.path) return 'Reading folders\u2026'
  if (d.ok === false) return `${d.path} \u2014 ${d.error || 'cannot be read'}`
  const n = (d.dirs || []).length
  return `${d.path} \u2014 ${n} ${n === 1 ? 'folder' : 'folders'}` +
    (d.files ? `, ${d.files} ${d.files === 1 ? 'file' : 'files'}` : '')
}

/* What picking a file fills in. Visible in the form and changeable, because the name
   is evidence and not an answer: `win11.iso` suggests a Windows guest, and somebody
   who knows better changes the field in front of them. */
function isoPatch(item) {
  const i = item || {}
  if (!i.path) return {}
  const patch = { imagePath: String(i.path), imageKind: i.kind || 'iso' }
  if (/win|windows|w1[01]|server20/i.test(String(i.name || ''))) patch.os = 'windows'
  return patch
}

/* The size this computer can give a guest, as one line, with the reason it gave for
   the first number. Blank fields in the form mean "take this". */
function fitWords(fit) {
  const f = fit || {}
  if (!f.cpus) return ''
  const gb = Math.round(((Number(f.memory_mb) || 0) / 1024) * 10) / 10
  const head = `for this computer: ${f.cpus} cores \u00b7 ${gb} GB memory \u00b7 ${f.disk_gb} GB disk`
  const why = (f.why || [])[0] || ''
  return why ? `${head} \u2014 ${why}` : head
}

function machineWords(m) {
  const st = String((m && m.state) || '')
  // `what` is the action, and it is read off the state rather than guessed at the
  // click. The old button tried a stop first and started only if the stop failed,
  // which meant pressing Start on a stopped machine stopped it (already stopped),
  // believed it had succeeded, and never started anything — a button that reports
  // "Stop X — done" and leaves you looking at a machine that did not move.
  if (st === 'running') return { chip: 'running', tone: 'run', verb: 'Stop', next: '', what: 'stop' }
  if (st === 'missing') return { chip: 'no distro yet', tone: 'need', verb: 'Create it', next: '', what: 'start' }
  if (st === 'provisioning') return { chip: 'making it…', tone: 'need', verb: '', next: 'provisioning', what: '' }
  return { chip: 'stopped', tone: '', verb: 'Start', next: '', what: 'start' }
}

/* What the machine is, in one line: which OS, from what, and roughly what it
   costs in disk. Never a claim about whether it *works* — that is the chip. */
function machineLine(m) {
  const bits = []
  if (!m) return ''
  if (m.provider === 'wsl') bits.push(`WSL · ${m.distro || 'no distro yet'}`)
  else bits.push('QEMU')
  const img = m.image || {}
  // The kind is said out loud for a QEMU machine: "iso" is the difference between a
  // machine that boots an installer and one that boots a disk somebody gave it.
  const file = img.path ? String(img.path).split(/[\\/]/).pop()
    : (img.url ? String(img.url).split('/').pop() : '')
  // A machine whose disk now holds a system is described by what it *is*: still calling it
  // "iso omarchy-4.0.4.iso" after the install would be the same lie in a smaller place.
  if (m.provider !== 'wsl' && m.installed) bits.push('its own system on disk')
  else if (file && m.provider !== 'wsl') bits.push(`${img.kind || 'disk'} ${file}`)
  else if (file) bits.push(file)
  const ports = machinePorts(m)
  if (ports.length) bits.push(ports.join(' '))
  if (m.attach) bits.push('your distro')
  if (m.cpus) bits.push(`${m.cpus} cores`)
  if (m.memory_mb) bits.push(`${Math.round(m.memory_mb / 1024)} GB`)
  return bits.join(' · ')
}

/* The question a missing prerequisite turns into. The command is the backend's,
   which knows the platform: a card that guessed `wsl --install` on Linux would be
   inventing a verb, and one that guessed the QEMU package would be wrong on two
   of three platforms. */
function needWords(n) {
  const d = n || {}
  return {
    what: String(d.needs_setup || 'something'),
    title: String(d.question || 'Something is missing'),
    detail: String(d.detail || ''),
    command: String(d.command || ''),
    then: String(d.then || ''),
    distros: Array.isArray(d.distros) ? d.distros : [],
  }
}

/* Whatever a response says is missing, as a list. `needs_setup` is one thing on the
   Machines tab (WSL or QEMU) and one thing per machine on a row, and a click that
   failed because of it has to be able to say *what* — an action that quietly does
   nothing is the worst answer available. Accepts a single object and an array of
   them, because the backend answers with whichever is honest at the time. */
function needList(value) {
  if (!value) return []
  const items = Array.isArray(value) ? value : [value]
  return items.filter((n) => n && typeof n === 'object')
}

/* Walking to a folder instead of typing one. Folders only, one level at a time, with a
   way up that stops at the root and a way in that is a click. "Use this folder" means
   the folder you are standing in, so nothing depends on what a text box says. The
   Windows dialog is offered when the app happens to expose one — it is not required,
   because a plugin cannot count on it. */
function FolderPicker({ ctx, start, onUse, onClose, busy }) {
  const [here, setHere] = useState(String(start || ''))
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [reading, setReading] = useState(false)

  const read = useCallback(async (path) => {
    setReading(true)
    try {
      const res = await ctx.rest('/machines/folders?path=' + encodeURIComponent(String(path || '')),
        { timeoutMs: 20000 })
      setData(res)
      setErr('')
      if (res && res.path) setHere(String(res.path))
    } catch (e) { setErr(String((e && e.message) || e)) } finally { setReading(false) }
  }, [ctx])
  useEffect(() => { read(start) }, [read, start])

  const d = data || {}
  const dirs = d.dirs || []
  const native = typeof window !== 'undefined' && window.hermesDesktop &&
    typeof window.hermesDesktop.selectPaths === 'function'
  const pickNative = useCallback(async () => {
    try {
      const picked = await window.hermesDesktop.selectPaths({ directories: true, multiple: false,
        title: 'Where the machine images live', defaultPath: here })
      const one = (picked && picked[0]) || (picked && picked.paths && picked.paths[0]) || ''
      if (one) onUse(String(one))
    } catch (e) { setErr(String(e)) }
  }, [here, onUse])

  return jsxs('div', { className: 'ah-notice', children: [
    jsx('div', { className: 'ah-sub', children: 'The folder your images live in. Only folders are ' +
      'listed, and Athena reads it when you point at it \u2014 never on its own.' }),
    jsxs('div', { className: 'ah-row', children: [
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: reading || !d.parent,
        onClick: () => read(d.parent), children: '\u2191 Up' }),
      jsx('span', { className: 'ah-sub', style: { flex: '1 1 auto', wordBreak: 'break-all' },
        children: reading ? 'reading\u2026' : folderWords(d) }),
    ] }),
    (d.drives || []).length > 1
      ? jsxs('div', { className: 'ah-row', children: [
          jsx('span', { className: 'ah-sub', children: 'drives' }),
          (d.drives || []).map((dr) => jsx(Button, { size: 'micro', variant: 'ghost',
            onClick: () => read(dr), children: dr }, 'dr' + dr)),
        ] })
      : null,
    jsx('div', { style: { display: 'flex', flexDirection: 'column', alignItems: 'flex-start',
      gap: 2, maxHeight: 240, overflow: 'auto', margin: '6px 0' }, children: dirs.length
      ? dirs.map((x) => jsx(Button, { size: 'micro', variant: 'ghost',
          onClick: () => read(x.path), children: '📁 ' + x.name }, x.path))
      : jsx('div', { className: 'ah-sub', children: 'no folders inside this one' }) }),
    err ? jsx('div', { className: 'ah-note', children: err }) : null,
    jsxs('div', { className: 'ah-row', children: [
      jsx(Button, { size: 'micro', disabled: !!busy || reading || d.ok === false,
        onClick: () => onUse(here), children: busy === 'iso-folder' ? 'pointing\u2026' : 'Use this folder' }),
      native ? jsx(Button, { size: 'micro', variant: 'ghost', onClick: pickNative,
        children: 'Windows folder dialog\u2026' }) : null,
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: onClose, children: 'Cancel' }),
    ] }),
  ] })
}

function SetupCard({ need, onInstall, onUseDistro, busy }) {
  const w = needWords(need)
  return jsxs('div', { className: 'ah-notice', children: [
    jsx('div', { className: 'ah-row', children: [
      jsx('span', { className: 'ah-badge need', children: 'needs setup' }),
      jsx('strong', { style: { fontSize: 12 }, children: w.title }),
    ] }),
    w.detail ? jsx('div', { className: 'ah-sub', children: w.detail }) : null,
    w.command
      ? jsxs('div', { className: 'ah-row', children: [
          jsx('code', { className: 'ah-term', style: { flex: '1 1 220px' }, children: w.command }),
          jsx(Button, { size: 'micro', disabled: !!busy, onClick: () => {
            try { navigator.clipboard.writeText(w.command) } catch (e) { /* no clipboard, no crime */ }
          }, children: 'Copy' }),
          jsx(Button, { size: 'micro', disabled: !!busy,
            onClick: () => onInstall(w.what), children: busy || 'Set it up' }),
        ] })
      : null,
    w.distros.length
      ? jsx('div', { className: 'ah-row', children: w.distros.map(n =>
          jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
            onClick: () => onUseDistro(n), children: n }, n)) })
      : null,
    w.then ? jsx('div', { className: 'ah-note', children: w.then }) : null,
  ] })
}

// Where a click on the picture lands inside the guest. The image is scaled to whatever
// width the card gives it, so the only honest way to turn a click into a guest coordinate
// is to ask the element how big it is and divide.
function guestPoint(rect, clientX, clientY, fw, fh) {
  /* Where in the guest a click on the picture landed.
     The picture is drawn with `object-fit: contain`, so the element's box and the picture are
     not the same rectangle: when the box is a different shape from the guest, the image is
     centred inside it and the difference above, below or beside it is empty space. Dividing
     by the box then reads a click near the top of the picture as further down the screen than
     it was, by exactly the size of that margin -- which is why a click on a menu item landed
     on the item below it whenever the guest was taller than the pane. So work out the drawn
     sub-rectangle first, then divide by it. When the box and the guest already agree -- the
     usual case -- this is a no-op. */
  const w = Number(fw) || 1, h = Number(fh) || 1
  const rw = (rect && rect.width) || w, rh = (rect && rect.height) || h
  const scale = Math.min(rw / w, rh / h)
  const padX = (rw - w * scale) / 2, padY = (rh - h * scale) / 2
  return {
    x: Math.max(0, Math.min(w - 1,
      Math.round(((clientX - (rect ? rect.left : 0) - padX) / (scale || 1))))),
    y: Math.max(0, Math.min(h - 1,
      Math.round(((clientY - (rect ? rect.top : 0) - padY) / (scale || 1))))),
  }
}

// One line above the picture: how big the guest's screen currently is and when the frame
// was read. A still picture with no timestamp is how somebody concludes a live machine is
// frozen.
function screenWords(frame, running) {
  const f = frame || {}
  if (f.error) return String(f.error)
  if (f.note) return String(f.note)
  if (!f.image) return running ? 'waiting for the first frame…' : 'not running'
  const when = String(f.at || '').split('T')[1] || ''
  return `${f.width}×${f.height} · ${Math.max(1, Math.round((f.bytes || 0) / 1024))} KB` +
         (when ? ` · ${when}` : '')
}

// Whether the tab should keep asking for frames: only while the picture is *in this pane* and
// the machine is running. A screen that was moved out into a window of its own is not this
// pane's picture, and reading it anyway was a full frame over VNC every 1.2 s for something
// nobody here is looking at — with the keys attached, which is worse than wasteful.
function screenLive(display_, running, shown) {
  return !!(shown && running && String(display_ || 'panel') === 'panel')
}

// How long to wait before asking for the next frame. What the person just did comes first,
// because their own input is the one thing on this screen that is *known* to have just
// changed: measured on this host, 2026-10-14, a keystroke leaves in about 20 ms and the guest
// has drawn its answer in about 139 ms — while the panel, patient by habit, was still waiting
// 1.2 s and then 2.5 s between looks, so letters took a second and a half to appear on a
// machine that had already drawn them. Input therefore buys a fast cadence for a moment.
// After that it is the guest's own behaviour: quick while the screen is changing, slower once
// two frames in a row have been byte-identical.
/* How often pointer motion may be forwarded while it is only hovering. A drag is the case
   that must feel live, and a tablet reports an absolute position, so the guest's pointer can
   simply follow the host's -- which is what "mouse passthrough" means and what the same guest
   does when it has a window of its own. 24ms is a little over one frame at 40Hz: fast enough
   that the guest's cursor never looks detached, slow enough not to make a round trip per
   pixel of travel. */
const POINTER_MOVE_MS = 24

/* The DOM's own button numbering, spelled out: 0 left, 1 middle, 2 right. */
const BUTTON_NAMES = { 0: 'left', 1: 'middle', 2: 'right' }

const INPUT_ECHO_MS = 80      // when to look after an event has gone out
const INPUT_BURST_MS = 1600   // how long a burst of typing keeps the fast cadence
const INPUT_FRAME_MS = 180    // the fast cadence itself: just above the guest's own echo
const STILL_FRAME_MS = 2500   // nothing is happening and nothing has for two frames
const MOVING_FRAME_MS = 1200  // something is happening that nobody asked for

function frameDelay(frame, still, sinceInput) {
  const f = frame || {}
  if (!f.image || f.error) return 2000
  const since = Number(sinceInput)
  if (Number.isFinite(since) && since >= 0 && since < INPUT_BURST_MS) return INPUT_FRAME_MS
  return Number(still) >= 2 ? STILL_FRAME_MS : MOVING_FRAME_MS
}

// Which key the guest should be told about: whatever the person's own keyboard said. No
// translation happens here — a shifted `@` arrives as `@`, and the guest is told "shift, 2"
// by the other side, because RFB has no keysym for `@` and inventing one sends nothing.
function screenKey(e) {
  return String((e && e.key) || '')
}

/* Who is holding the keyboard, and how it changes hands. A value, so the driver reads the rule
   instead of a side effect: `screenKey` is what the guest is told, this is what the *person* is
   offered.

   Ctrl+Alt on its own is the way out, and it is deliberately two-step. Pressing the pair is not
   the release: Ctrl+Alt+F2 has to reach the guest, and it does the instant the third key arrives
   — that is what 'no' means, and it is why the release cannot happen on the way down. It fires
   on the way UP, when both modifiers are already off and nothing else was pressed in between.
   Every combination stays in the guest's hands and the person still has a way out that is not
   the mouse. */
function keyOwnerStep(owner, key, e, phase) {
  const modifier = ['Shift', 'Control', 'Alt', 'Meta', 'CapsLock'].includes(key)
  if (phase === 'down') {
    if (modifier) return { owner: (e && e.ctrlKey && e.altKey) ? 'maybe' : owner, release: false }
    return { owner: 'no', release: false }        // a combination is being typed, not a way out
  }
  const lettingGo = key === 'Control' || key === 'Alt'
  const stillHeld = !!(e && (e.ctrlKey || e.altKey))
  if (owner === 'maybe' && lettingGo && !stillHeld) return { owner: 'no', release: true }
  return { owner, release: false }
}

/* What the dock says, in the words somebody needs at the moment they need them. Both states name
   the way back, and the pointer sentence is in both because the pointer is the thing a person
   cannot reason about from the picture: it is never captured, so there is always a way out. */
function dockNotes(kb) {
  return {
    key: kb
      ? 'This picture has the keyboard: type freely, and combinations like Ctrl+Alt+F2 or Super+Enter go to the VM.'
      : 'The keyboard is back on this desktop.',
    mouse: 'The pointer is never captured \u2014 move it off the picture and it is yours again.',
    how: kb ? 'Ctrl+Alt on its own gives the keyboard back.'
            : 'Click the picture, or the button, to hand it to the VM.',
  }
}

function keyboardButton(kb) { return kb ? 'Release keyboard' : 'Take keyboard' }

/* Who is actually driving this guest, as the dock's second line.

   `keyboardNote` above is about where the *browser's* keystrokes go; this is about the guest's
   one keyboard, which is a different question and the one that had no answer. Written as a
   plain function of the frame's `lease` field so the driver can draw every state it has, and
   so the three things that make this worth a line are decided in one place:

     * An agent driving says so, and carries the one button that stops it. A person told "an
       agent is typing here" with no way to stop it has been given a problem, not a control.
     * A person driving gets no button. There is nothing to take back — the person whose hand
       is on the keyboard is the one this whole rule exists to protect — and a button that
       would refuse is worse than no button.
     * Nobody driving draws nothing at all. An empty has-nobody-held-it line on every screen
       would train people to stop reading the line that matters.
     * The note names the machine, because the frame that carries it can be a tab that was
       left open on another one. */
function leaseWords(lease, name) {
  const l = lease || null
  const who = l && l.who
  if (!who) return null
  const machine = name || 'this machine'
  if (who === 'agent') {
    const held = (typeof l.for_seconds === 'number' && l.for_seconds >= 0)
      ? ` It has had it ${Math.round(l.for_seconds)}s.` : ''
    return {
      who,
      text: `An agent is driving this screen and answering for you.${held}`,
      button: 'Take it back',
      title: `An agent holds ${machine}'s keyboard. Taking it back hands the keyboard to you, `
        + `and the agent's next keystroke is refused until it asks again.`,
    }
  }
  return {
    who,
    text: `You are driving this screen — an agent's keystrokes are refused while you have it.`,
    button: '',
    title: `A person holds ${machine}'s keyboard. It is let go on its own after ten quiet `
      + `minutes, or when you stop touching the picture.`,
  }
}

/* The line the dock draws, as one string, so the panel has one place to change it and the
   driver has one thing to read. */
function keyboardNote(kb) {
  const n = dockNotes(kb)
  return `${n.key} ${n.mouse} ${n.how}`
}

function GuestScreen({ ctx, name, frame, onWarn, kick }) {
  const box = useRef(null)
  const last = useRef(0)
  const spot = useRef({ x: 0, y: 0 })      // where the pointer was, for a release with no position
  const owner = useRef('no')               // the Ctrl+Alt gesture's two-step state
  /* Which buttons the guest has been told are down, and which DOM button number is which. The
     panel used to send `['left']` whatever was pressed, so a middle click and a right click both
     arrived as a left click; and it tracked "is something held" as a single boolean, which cannot
     say *what*. */
  const buttons = useRef([])
  const inFlight = useRef(null)            // the input request that is out right now
  const waiting = useRef([])               // events queued behind it, in the order they happened
  const [kb, setKb] = useState(true)       // does the guest hold the keyboard right now
  const [taking, setTaking] = useState(false)
  const f = frame || {}
  // Who has the guest's keyboard: nobody, a person, or an agent. Drawn by the dock, and the
  // one control on this screen that is about somebody else's hands rather than this person's.
  const lw = leaseWords(f.lease, name)

  /* Taking the keyboard back. This is a POST to the panel's own lease route, and it is here
     rather than in the tab because the button lives in the dock — and because a person who has
     just watched an agent type over their installer should not have to find another screen to
     stop it. A refusal is reported rather than swallowed: `res.ok === false` on this route is
     the only way to learn the release did not happen. */
  const takeBack = async () => {
    setTaking(true)
    try {
      const res = await ctx.rest(`/machines/${encodeURIComponent(name)}/lease/release`,
        { method: 'POST', timeoutMs: 20000 })
      const why = actionFailure('Take the keyboard back', res)
      if (why && onWarn) onWarn(why)
    } catch (e) {
      if (onWarn) onWarn(`Take the keyboard back: ${String((e && e.message) || e)}`)
    } finally { setTaking(false) }
  }

  /* `kick` arrives as a prop, because the read loop this asks to hurry lives one component up.
     This half sends input and draws the frame it is handed; the timer, the live flag and the
     tick belong to whoever owns the loop — MachineScreenTab. They were declared here once and
     used there, and a ref named nowhere in the function that names it is a ReferenceError the
     moment the tab mounts: "liveRef is not defined", and the whole workspace fails to render.
     A ref is a name in one scope; moving the name is what fixes it, not declaring it twice. */

  const post = useCallback(async (events) => {
    kick()
    try {
      const res = await ctx.rest(`/machines/${encodeURIComponent(name)}/screen/input`,
        { method: 'POST', body: { events }, timeoutMs: 8000 })
      // `ok` is not the same as `typed`: the route reports what it could not turn into a
      // keystroke, and a guest that never got a combination is worth a sentence.
      // The lease refuses an agent's keystroke here with `ok: false` and a reason
       // (`human_has_control`), and a refusal nobody can read is a keystroke that vanished.
       // This is the one place the panel meets that answer, so it says it.
      const why = actionFailure('That keystroke', res)
      if (why) { if (onWarn) onWarn(why); else notify(ctx, 'error', why) }
      else if (onWarn && res && res.dropped && res.dropped.length) {
        onWarn(`the guest did not get: ${res.dropped.map((d) => d.why).join('; ')}`)
      }
      kick()
    } catch (e) {
      if (onWarn) onWarn(`input did not reach the guest: ${String(e && e.message || e)}`)
    }
  }, [ctx, name, onWarn, kick])

  /* One keystroke was one HTTP round trip of its own, fired the moment the DOM event happened,
     so a fast typist had several in the air at once and the guest saw them in whatever order the
     replies came back. Now at most one batch is in flight and everything the person does while
     it is out joins the queue behind it, in order, as a single request the moment it lands. */
  const send = useCallback(async (events) => {
    if (!events || !events.length) return
    if (inFlight.current) { waiting.current.push(...events); return inFlight.current }
    inFlight.current = (async () => {
      await post(events)
      while (waiting.current.length) {
        await post(waiting.current.splice(0, waiting.current.length))
      }
    })().then(() => { inFlight.current = null },
             () => { inFlight.current = null })
    return inFlight.current
  }, [post])

  const pointOf = (e) => guestPoint(box.current ? box.current.getBoundingClientRect() : null,
                                    e.clientX, e.clientY, f.width, f.height)

  /* Handing the keyboard back is a first-class action, not a side effect of clicking somewhere
     else. It blurs the picture -- which is what actually stops this tab's window listener
     forwarding keys -- and says so in the dock. The pointer never needs releasing, because it
     is never captured; see dockNotes. */
  const release = useCallback(() => {
    const el = box.current
    if (el && typeof el.blur === 'function') el.blur()
    setKb(false)
  }, [])
  const take = useCallback(() => {
    const el = box.current
    if (el && typeof el.focus === 'function') el.focus()
    setKb(true)
  }, [])
  const step = (e, phase) => {
    const key = screenKey(e)
    if (!key) return false
    const next = keyOwnerStep(owner.current, key, e, phase)
    owner.current = next.owner
    if (!next.release) return false
    release()
    return true
  }

  const onDown = (e) => {
    const name = BUTTON_NAMES[e.button] || 'left'
    const p = pointOf(e)
    spot.current = p
    setKb(true)
    if (box.current) box.current.focus()
    if (buttons.current.indexOf(name) < 0) buttons.current = buttons.current.concat([name])
    send([{ kind: 'pointer', buttons: buttons.current, x: p.x, y: p.y }])
  }
  /* Marked on both events, because there are two: React hands this handler a synthetic event
     whose `.nativeEvent` is the one the tab's window listener will also see. Marking only the
     synthetic one sent every key twice -- "zack" arrived as "zzaacckk". */
  const onKeyHere = (e) => {
    e.__athenaSent = true
    if (e.nativeEvent) e.nativeEvent.__athenaSent = true
    onKey(e)
  }
  /* The other half of the gesture: whether Ctrl+Alt was a way out depends on what happens when
     it is let go, so the key-up has to be heard even when focus has already moved. */
  const onKeyUp = (e) => { step(e, 'up') }
  const onUp = (e) => {
    const name = BUTTON_NAMES[e.button] || 'left'
    const p = pointOf(e)
    spot.current = p
    buttons.current = buttons.current.filter((b) => b !== name)
    send([{ kind: 'pointer', buttons: buttons.current, x: p.x, y: p.y }])
  }
  /* Leaving the picture lets go of everything, not of one button: a drag that ends outside it must
     not leave the guest holding a button the person's mouse is no longer on, and a leave event's
     `button` says nothing about what was down. */
  const onLeave = () => {
    if (!buttons.current.length) return
    buttons.current = []
    const p = spot.current
    send([{ kind: 'pointer', buttons: [], x: p.x, y: p.y }])
  }
  /* Motion goes to the guest whether or not a button is down. Requiring one is why the docked
     picture could only be used by pressing and dragging: the guest's pointer moved only while the
     host's button was held, so hovering did nothing and the next click landed wherever the last
     drag had left it. It is throttled rather than forbidden -- `POINTER_MOVE_MS` -- and a hover
     that has not changed the guest's coordinates is not a round trip. */
  const onMove = (e) => {
    const now = Date.now()
    if (now - last.current < POINTER_MOVE_MS) return
    const p = pointOf(e)
    if (!buttons.current.length && p.x === spot.current.x && p.y === spot.current.y) return
    last.current = now
    spot.current = p
    send([{ kind: 'pointer', buttons: buttons.current, x: p.x, y: p.y }])
  }
  /* React attaches `wheel` as a passive listener, so the `preventDefault` this handler used to
     call was a promise the browser had already decided not to keep: the guest scrolled *and* the
     panel scrolled under it. A native listener with `passive: false` is the only way to actually
     keep the scroll where it was aimed, so the handler lives in an effect and the `<img>` no
     longer takes an `onWheel`. */
  const onWheelHere = useCallback((e) => {
    if (e.preventDefault) e.preventDefault()
    const p = pointOf(e)
    spot.current = p
    send([{ kind: 'wheel', wheel: e.deltaY > 0 ? 'down' : 'up', x: p.x, y: p.y }])
  }, [send, pointOf])
  /* The app's context menu is not what a right click over a guest's screen is for. */
  const onContext = (e) => { if (e.preventDefault) e.preventDefault() }
  const onKey = (e) => {
    const key = screenKey(e)
    if (!key) return
    if (step(e, 'down')) return
    if (['Shift', 'Control', 'Alt', 'Meta', 'CapsLock'].includes(key)) return
    if (e.preventDefault) e.preventDefault()
    /* Every modifier that was held travels with the key. This used to test Ctrl and Alt only, so
       Super+Enter arrived at the guest as a bare Enter -- which is not a keystroke that did
       nothing, it is a *different* keystroke, acted on by the guest. `e.metaKey` is the
       Windows/Command key, and the guest keysym for it is Super (0xFFEB), not Meta: a desktop
       like Omarchy opens its overview on Super_L, and Meta_L is a key nothing listens for. */
    if (e.ctrlKey || e.altKey || e.metaKey) {
      const mods = []
      if (e.ctrlKey) mods.push('Control')
      if (e.altKey) mods.push('Alt')
      if (e.metaKey) mods.push('Super')
      if (e.shiftKey) mods.push('Shift')
      send([{ kind: 'combo', mods, key }])
      return
    }
    send([{ kind: 'key', key }])
  }

  /* Typing has to work without a click in front of every letter.
     The picture used to be the only thing listening, so it needed focus -- and focus in a panel
     moves: an installer that asks for a hostname, a password and a username was one click per
     keystroke, which is not a computer anybody can set up. So the tab listens on the window for
     as long as it is open, and the picture keeps its own handler for the combinations. */
  const onWindowKey = useCallback((e) => {
    if (e.__athenaSent) return                       // the picture's own handler already had it
    const el = e.target
    /* The second half of the same guard, in case a runtime hands the two handlers events that
       share nothing: a key whose target is the picture has already been sent by the picture. */
    if (el && el.classList && typeof el.classList.contains === 'function'
        && el.classList.contains('ah-screenimg')) return
    if (el && typeof el.closest === 'function'
        && el.closest('input,textarea,select,[contenteditable="true"]')) return
    /* The app's own shortcuts stay the app's -- but Super is neither the app's nor the panel's.
       On a desktop like Omarchy, Super *is* the keyboard, and a Super+Enter that never arrives is
       reported as "the shortcut acted on Hermes instead of the guest". So a combination that
       carries Meta goes through; Ctrl and Alt on their own stay the app's (the picture's own
       handler sends those once the picture has focus, and Ctrl+Alt on its own is the way out). */
    if ((e.ctrlKey || e.altKey) && !e.metaKey) return
    /* Only a key that is *this picture's business*: aimed at it, or pressed while it is the
       thing holding the keyboard. Without this, a letter typed into the chat with this tab open
       would go to the guest as well — twice is worse than once, and a guest that logs the
       host's password into a terminal it does not have is not better either. */
    const mine = (n) => !!(n && typeof n.closest === 'function' && n.closest('.ah-screendock'))
    if (!mine(el) && !mine(document.activeElement)) return
    e.__athenaSent = true
    onKey(e)
  }, [send])

  useEffect(() => {
    /* Letting go of the window is the same moment as letting go of a button: a drag that ends
       outside the picture -- past the edge of the screen, over the chat -- never delivers the
       mouseup the guest is waiting for, and the guest is left holding the left button while the
       person's mouse is back on their desktop. Nobody sees that mouseup, which is exactly why it
       has to be caught here, on the window, for as long as this tab is open. */
    const onAnyUp = () => {
      if (!buttons.current.length) return
      buttons.current = []
      const p = spot.current
      send([{ kind: 'pointer', buttons: [], x: p.x, y: p.y }])
    }
    window.addEventListener('keydown', onWindowKey)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('mouseup', onAnyUp)
    return () => {
      window.removeEventListener('keydown', onWindowKey)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('mouseup', onAnyUp)
    }
  }, [onWindowKey, onKeyUp, send])

  /* Hermes' chat composer has a reflex called type-to-focus: a printable key that lands on
     ordinary chrome focuses the composer and types there. That is the reported bug in one
     sentence -- "it automatically resets into our session for me to type over here" -- and the
     app documents the way out: focus inside a `[data-terminal]` scope keeps its keys, which is
     what its own remote-screen pane marks itself with. So the picture claims that scope, and
     opening the tab puts focus in it without a click (politely: only when nothing is being
     typed). `data-remote-screen` is the sibling marker that tells the close-tab router this is
     not a local terminal. */
  useEffect(() => {
    const el = box.current
    if (!el) return
    const a = document.activeElement
    if (!a || a === document.body || a === document.documentElement) el.focus()
  }, [])

  const hasPic = !!f.image
  /* `passive: false` is the whole point of this effect: see onWheelHere. */
  useEffect(() => {
    const el = box.current
    if (!el || !hasPic) return undefined
    el.addEventListener('wheel', onWheelHere, { passive: false })
    return () => el.removeEventListener('wheel', onWheelHere)
  }, [hasPic, onWheelHere])

  return jsxs('div', { className: 'ah-screendock', 'data-terminal': '', 'data-remote-screen': '',
                       children: [
    f.image
      ? jsx('img', {
          className: 'ah-screenimg', src: f.image, alt: `the screen of ${name}`,
          'data-terminal': '', 'data-remote-screen': '',
          tabIndex: 0, ref: box,
          onMouseDown: onDown, onMouseUp: onUp, onMouseLeave: onLeave, onMouseMove: onMove,
          onKeyDown: onKeyHere, onKeyUp, onFocus: () => setKb(true),
          onBlur: () => setKb(false), onContextMenu: (e) => e.preventDefault(),
        })
      : jsx('div', { className: 'ah-screenblank', children: f.note || f.error || 'no frame yet' }),
    jsxs('div', { className: 'ah-notebar', children: [
      jsx('div', { className: 'ah-note', children: keyboardNote(kb) }),
      /* A button, not a key. The person who has lost the mouse is not the person who needs a
         gesture, and the person whose keyboard is in the guest can still click. Either way out
         is one click from the other, and neither one depends on the other working. */
      jsx('button', { className: 'ah-kbswitch', type: 'button', onClick: kb ? release : take,
                      children: keyboardButton(kb) }),
    ] }),
    /* Who else is on this keyboard. Its own row rather than a word inside the line above,
       because these are two different questions — where this browser's keys go, and who the
       guest is listening to — and the second one only exists while somebody is answering for
       you. Nothing is drawn when nobody holds it: see leaseWords. */
    lw
      ? jsxs('div', { className: 'ah-notebar', children: [
          jsx('div', { className: 'ah-note', title: lw.title, children: lw.text }),
          lw.button
            ? jsx('button', { className: 'ah-kbswitch', type: 'button', title: lw.title,
                      disabled: taking, 'data-lease-release': name, onClick: takeBack,
                      children: taking ? 'Taking it\u2026' : lw.button })
            : null,
        ] })
      : null,
  ] })
}

/* A machine's screen, at panel size.

   Deliberately not a thumbnail in a card. A guest is a computer, and the thing you are
   installing on is something you look at properly — a 640x480 installer squeezed into a
   card is a picture of a computer. The picture itself is the same one the card used to
   draw: a PNG read server-side over VNC, because a panel is a browser context and cannot
   open a socket to a guest. What is different is the size, and that the two things worth
   leaving the tab for are here: sending the screen to a window of its own, and typing into
   it (GuestScreen does the input half at any size). */
/* A machine with a medium attached starts somewhere, and the tab says where and offers the
   other way — `medium` first for a live or rescue image, the disk first otherwise. Pure, so
   the words for both states can be read without a machine. */
function bootChoice(row) {
  if (!row || row.provider !== 'qemu' || !row.image || row.image.kind !== 'iso') return null
  const onMedium = String(row.boot || '') === 'medium'
  if (onMedium) {
    return { now: 'it starts on the installer ISO every time', next: 'Start on its own system' }
  }
  // An installed machine is not an installer any more: the medium is off the command line
  // entirely, and saying so is the difference between a decision and a mystery.
  if (row.installed) {
    return { now: 'it holds an installed system, and it starts on that',
             next: 'Attach the installer again' }
  }
  return { now: 'it starts on what its disk holds, and the installer only if the disk cannot boot',
           next: 'Start on the installer' }
}

/* When input asks the reader for a fresh picture, the earliest look already asked for is the one
   that matters. `kick` used to clear the pending timer and re-arm it from the clock, so a person
   typing faster than INPUT_ECHO_MS cleared the timer before it could ever fire: `tick` — the only
   thing that reads a frame — did not run at all until they stopped typing, and the picture went
   stale exactly while they were looking at it. That is the reported lag, and the fix is that a
   pending look is never pushed later than it already is: `null` means leave it alone, a number
   means the look was further away than an echo, so bring it in. Pure, so the rule can be read
   without a machine. */
function kickDelay(prevDeadline, now) {
  if (prevDeadline > now && prevDeadline <= now + INPUT_ECHO_MS) return null
  return INPUT_ECHO_MS
}

function MachineScreenTab({ ctx }) {
  const name = useValue(watchMachine)
  const [row, setRow] = useState(null)
  const [frame, setFrame] = useState(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState('')
  // Two frames in a row that were byte-identical mean the guest has stopped moving, so the
  // next look can wait. Same rule as the card's reader: the delay follows the guest.
  const still = useRef({ n: 0, bytes: -1 })
  const lastInput = useRef(0)      // when the person last touched this machine
  const timerRef = useRef(null)    // the reader's pending look
  const deadlineRef = useRef(0)    // when that look is due, so `kick` can tell sooner from later
  const liveRef = useRef(false)
  const tickRef = useRef(null)

  /* Input asks for the picture back. Everything the person does goes through the picture —
     a key, a click, a drag, the wheel — so one call covers them all, and it is what turns
     "the machine answers immediately" into "the panel shows the answer immediately". The
     picture calls this; the loop above owns the refs it moves. */
  const kick = useCallback(() => {
    lastInput.current = Date.now()
    if (!liveRef.current || !tickRef.current) return
    const now = Date.now()
    const wait = kickDelay(deadlineRef.current, now)
    if (wait === null) return              // a look is already coming at least this soon
    if (timerRef.current) clearTimeout(timerRef.current)
    deadlineRef.current = now + wait
    timerRef.current = setTimeout(() => {
      deadlineRef.current = 0
      if (tickRef.current) tickRef.current()
    }, wait)
  }, [])

  const load = useCallback(async () => {
    try {
      const d = await ctx.rest('/machines', { timeoutMs: 15000 })
      const all = (d && (d.machines || d.items)) || []
      setRow(all.find((x) => x.name === name) || null)
    } catch (e) { setNote(`reading the machine: ${String((e && e.message) || e)}`) }
  }, [ctx, name])

  useEffect(() => {
    load()
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [load])

  const running = !!(row && row.state === 'running')
  const display_ = row ? row.display : ''

  useEffect(() => {
    still.current = { n: 0, bytes: -1 }
    setFrame(null)
    // Nothing to ask a stopped machine for, and nothing to read while its screen is a
    // window of its own — the picture would be the same one and the guest would not be
    // in it.
    if (!screenLive(display_, running, true)) {
      liveRef.current = false
      tickRef.current = null
      return undefined
    }
    let live = true
    liveRef.current = true
    const tick = async () => {
      deadlineRef.current = 0
      let snap = null
      try {
        snap = await ctx.rest(`/machines/${encodeURIComponent(name)}/screen`,
          { timeoutMs: 20000 })
      } catch (e) { snap = { error: String((e && e.message) || e) } }
      if (!live) return
      setFrame(snap)
      const prev = still.current
      still.current = { n: snap && snap.bytes && snap.bytes === prev.bytes ? prev.n + 1 : 0,
                        bytes: (snap && snap.bytes) || -1 }
      const wait = frameDelay(snap, still.current.n, Date.now() - lastInput.current)
      deadlineRef.current = Date.now() + wait
      timerRef.current = setTimeout(tick, wait)
    }
    tickRef.current = tick
    tick()
    return () => {
      live = false
      liveRef.current = false
      tickRef.current = null
      if (timerRef.current) clearTimeout(timerRef.current)
      deadlineRef.current = 0
    }
  }, [ctx, name, running, display_])

  /* Where the screen lives is QEMU's decision, made on its command line, so moving it is a
     restart — and the reply says which happened rather than leaving the click to look like
     it missed. */
  const onPopOut = useCallback(async () => {
    const want = display_ === 'window' ? 'panel' : 'window'
    setBusy('popout')
    try {
      const res = await ctx.rest(`/machines/${encodeURIComponent(name)}/display`,
        { method: 'POST', timeoutMs: 120000, body: { display: want } })
      const why = actionFailure('Move the screen', res)
      setNote(why || `${name} — ${(res && res.note) || ''}`)
      await load()
    } catch (e) { setNote(`Move the screen: ${String((e && e.message) || e)}`) } finally { setBusy('') }
  }, [ctx, name, display_, load])

  /* What a machine with a medium attached starts on. The reported shape of getting this wrong:
     an install finishes, says "reboot now", and the guest boots the installer again -- to the
     person watching, an install that vanished. The default is already the disk first; this is
     for the other direction, a live or rescue medium that is the point of the machine. */
  const onBoot = useCallback(async () => {
    const want = String((row && row.boot) || '') === 'medium' ? 'system' : 'medium'
    setBusy('boot')
    try {
      const res = await ctx.rest(`/machines/${encodeURIComponent(name)}/boot`,
        { method: 'POST', timeoutMs: 180000, body: { boot: want } })
      const why = actionFailure('Change what it boots', res)
      setNote(why || `${name} — ${(res && res.note) || ''}`)
      await load()
    } catch (e) { setNote(`Change what it boots: ${String((e && e.message) || e)}`) } finally { setBusy('') }
  }, [ctx, name, row, load])

  const onAct = useCallback(async (what) => {
    setBusy(what)
    try {
      // Two named routes rather than one assembled on the fly: a route checker that reads
      // the URL out of the source (tests/live/t_ui_routes.py) can only see a literal, and
      // `/machines/{}/{}` is a URL nobody declares.
      const res = what === 'start'
        ? await ctx.rest(`/machines/${encodeURIComponent(name)}/start`,
            { method: 'POST', timeoutMs: 120000 })
        : await ctx.rest(`/machines/${encodeURIComponent(name)}/stop`,
            { method: 'POST', timeoutMs: 120000 })
      const why = actionFailure(`${what === 'start' ? 'Start' : 'Stop'} ${name}`, res)
      setNote(why || '')
      await load()
    } catch (e) { setNote(`${what}: ${String((e && e.message) || e)}`) } finally { setBusy('') }
  }, [ctx, name, load])


  // Closing the tab is closing the screen: the tab is the watch, and there is no third
  // state where a machine is open and nothing is pointed at it.
  const close = () => { watchMachine.set(null); tab.set('machines') }
  const w = machineWords(row || {})

  return jsxs('div', { className: 'ah-mscreen', children: [
    jsxs('div', { className: 'ah-row', children: [
      jsx('div', { className: 'ah-mtitle', children: name || 'no machine' }),
      jsx('span', { className: 'ah-state' + (w.tone ? ' ' + w.tone : ''), children: w.chip }),
      jsx('span', { className: 'ah-note', children: screenWords(frame, running) }),
      jsxs('div', { className: 'ah-mbtns', children: [
        (row && row.provider === 'qemu')
          ? jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy, onClick: onPopOut,
              children: busy === 'popout' ? 'Moving it…'
                : (display_ === 'window' ? 'Bring it back into the panel' : 'Open in a new window') })
          : null,
        bootChoice(row)
          ? jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy, onClick: onBoot,
              title: 'Where this machine starts. The default is its own disk, falling back to '
                + 'the installer when the disk cannot boot. Changing it restarts a running '
                + 'machine.',
              children: busy === 'boot' ? '…' : bootChoice(row).next })
          : null,
        jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
          onClick: () => onAct(running ? 'stop' : 'start'),
          children: busy === 'start' || busy === 'stop' ? '…' : (running ? 'Stop' : 'Start') }),
        jsx(Button, { size: 'micro', variant: 'ghost', onClick: close, children: 'Close' }),
      ] }),
    ] }),
    note ? jsx('div', { className: 'ah-note', children: note }) : null,
    jsx('div', { className: 'ah-mbody', children: [
      screenLive(display_, running, true)
        ? jsx(GuestScreen, { ctx, name, frame, onWarn: setNote, kick })
        : jsx('div', { className: 'ah-screenblank', children: row
            ? (display_ === 'window'
              ? 'its screen is a window of its own, drawn there and not here — bring it back '
                + 'into the panel to see it in this tab'
              : 'nothing to show — it is not running')
            : 'reading the machine…' }),
    ] }),
    jsx('div', { className: 'ah-note', children: [
      (frame && frame.width)
        ? jsx('span', { children: `its screen is ${frame.width}×${frame.height} · ` })
        : null,
      bootChoice(row) ? jsx('span', { children: bootChoice(row).now + ' · ' }) : null,
      (row && row.reach) ? row.reach
        : 'Its screen is read over VNC by this plugin and drawn here: clicks, keystrokes and the '
          + 'wheel reach the guest while this tab is open.'] })
  ] })
}

/* ------------------------------------------------------------ a machine's settings */
/* What a machine already is, as text a box can hold. Pure and handed the machine rather than
   the tab's state, so the driver can draw this from a fixture — and so the form and the change
   it builds can never disagree about what "unchanged" means. */
function machineFields(m) {
  const x = m || {}
  const ssh = x.ssh || {}
  return {
    cpus: String(x.cpus == null ? '' : x.cpus),
    memory_mb: String(x.memory_mb == null ? '' : x.memory_mb),
    disk_gb: String(x.disk_gb == null ? '' : x.disk_gb),
    display: String(x.display == null ? '' : x.display),
    boot: String(x.boot == null ? '' : x.boot),
    ssh_port: ssh.port ? String(ssh.port) : '',
    ports: ((x.ports || []).map((q) => `${q.host}:${q.guest}`)).join('\n'),
    notes: String(x.notes == null ? '' : x.notes),
  }
}

/* Ports, as a person writes them and as the spec stores them — one reader for both directions.
   Two ways round is a guest that boots and is unreachable, so `8080:80` is host 8080, guest 80:
   you type the one on your computer first, because that is the one you will open. It answers
   with the pairs *and* the pieces that were not pairs, because two callers want different
   things from the same text: making a machine from a form drops what it cannot read, while
   changing a machine that already exists refuses the line and names it — a port somebody typed
   and the machine quietly did not open is a machine that looks broken a week later, at the
   moment whatever is inside it matters. */
function portPairs(text) {
  const pairs = [], bad = []
  String(text == null ? '' : text).split(/[,\s]+/).forEach((raw) => {
    const bit = String(raw).trim()
    if (!bit) return
    const hit = /^([0-9]{1,5})\s*(?:->|[:>=-])\s*([0-9]{1,5})$/.exec(bit)
    const host = hit ? Number(hit[1]) : NaN, guest = hit ? Number(hit[2]) : NaN
    if (!hit || !(host >= 1 && host <= 65535) || !(guest >= 1 && guest <= 65535)) { bad.push(bit); return }
    pairs.push({ host, guest })
  })
  return { pairs, bad }
}

/* The changes to send: only what is different from what the machine already is. A form that
   posts every field back is a form that overwrites the values it was never asked about, and the
   numbers here are the ones a person typed into a box that may hold anything at all. */
function machineChanges(fields, m) {
  const was = machineFields(m || {})
  const now = fields || was
  const same = (k) => String(now[k] == null ? '' : now[k]).trim() === String(was[k] == null ? '' : was[k]).trim()
  const canon = (text) => portPairs(text).pairs.map((q) => `${q.host}:${q.guest}`).join(',')
  const whole = (k, unit) => {
    const text = String(now[k] == null ? '' : now[k]).trim()
    const n = Number(text)
    if (!text || !isFinite(n) || n !== Math.floor(n) || n <= 0) {
      return { error: `${unit} has to be a whole number — "${text}" is not one` }
    }
    return { value: n }
  }
  const changes = {}
  for (const [key, unit] of [['cpus', 'cores'], ['memory_mb', 'memory'], ['disk_gb', 'the disk size']]) {
    if (same(key)) continue
    if (!String(now[key] == null ? '' : now[key]).trim()) continue   // blank leaves it alone
    const got = whole(key, unit)
    if (got.error) return { error: got.error }
    if (key === 'disk_gb') {
      const had = Number(was.disk_gb) || 0
      if (had && got.value < had) {
        return { error: `a disk can be grown and not shrunk — ${got.value} GB is smaller than the ` +
                        `${had} GB it already is` }
      }
    }
    changes[key] = got.value
  }
  if (!same('display')) changes.display = String(now.display || '')
  if (!same('boot')) changes.boot = String(now.boot || '')
  if (!same('ssh_port')) {
    const text = String(now.ssh_port == null ? '' : now.ssh_port).trim()
    if (!text) changes.ssh = null                      // no ssh port is a setting, not a blank
    else {
      const n = Number(text)
      if (!isFinite(n) || n !== Math.floor(n) || n < 1 || n > 65535) {
        return { error: `the ssh port has to be a number under 65536 — "${text}" is not one` }
      }
      changes.ssh = { port: n }
    }
  }
  // Compared as pairs, not as text: the same two ports written `8080:80, 9000:9000` and
  // `8080:80\n9000:9000` are the same machine, and a form that called them different would
  // rewrite the ports on every save.
  if (canon(now.ports) !== canon(was.ports)) {
    const parsed = portPairs(now.ports)
    if (parsed.bad.length) {
      return { error: `these are not host:guest port pairs: ${parsed.bad.join(', ')} — ` +
                      'write one per line, like 8080:80' }
    }
    changes.ports = parsed.pairs
  }
  if (!same('notes')) changes.notes = String(now.notes == null ? '' : now.notes)
  return { changes }
}

/* Settings for a machine that already exists. The rules are the backend's — this form does not
   decide what a value may be, it types one and reports what came back, which is the only way the
   panel and an agent can be told the same sentence about the same machine. */
function MachineSettings({ ctx, m, fit, onClose, onSaved }) {
  const [f, setF] = useState(() => machineFields(m))
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [bad, setBad] = useState('')
  const set = (k) => (e) => setF(Object.assign({}, f, { [k]: e.target.value }))
  const maxCpus = (fit && fit.cpus) || 0
  const maxMem = (fit && fit.memory_mb) || 0
  const save = async () => {
    const built = machineChanges(f, m)
    if (built.error) { setBad(built.error); return }
    const keys = Object.keys(built.changes)
    if (!keys.length) { setBad(''); setNote('nothing here is different from what it already is'); return }
    setBad(''); setBusy(true)
    try {
      const r = await ctx.rest(`/machines/${encodeURIComponent(m.name)}`,
        { method: 'PATCH', timeoutMs: 300000, body: { changes: built.changes } })
      if (r && r.ok === false) { setBad((r && r.error) || 'that did not take'); return }
      setNote((r && r.note) || `${keys.join(', ')} saved`)
      if (onSaved) onSaved(r)
    } catch (e) { setBad(String((e && e.message) || e)) } finally { setBusy(false) }
  }
  const num = (label, key, hint, extra) => jsxs('label', Object.assign({ className: 'ah-namefield' },
    extra || {}, { children: [
      jsx('span', { className: 'ah-flabel', children: label }),
      jsx('input', { className: 'ah-namein', type: 'text', inputMode: 'numeric', value: f[key],
        placeholder: hint, onChange: set(key) }),
    ] }))
  const pick = (label, key, options) => jsxs('label', { className: 'ah-namefield', children: [
    jsx('span', { className: 'ah-flabel', children: label }),
    jsx('select', { className: 'ah-searchin', value: f[key], onChange: set(key),
      children: options.map(([value, text]) => jsx('option', { value, children: text }, value || 'auto')) }),
  ] })
  return jsxs('div', { className: 'ah-detail ah-settings', children: [
    jsxs('div', { className: 'ah-row', children: [
      num('Cores', 'cpus', maxCpus ? `up to ${maxCpus} on this computer` : 'blank leaves it as it is'),
      num('Memory (MB)', 'memory_mb', maxMem ? `up to ${maxMem} MB on this computer` : 'blank leaves it as it is'),
      num('Disk (GB)', 'disk_gb', 'can grow, never shrink'),
    ] }),
    jsxs('div', { className: 'ah-row', children: [
      pick('Display', 'display', [['', 'decide when it starts'], ['panel', 'in the panel'],
        ['window', 'its own window'], ['none', 'no screen']]),
      pick('Starts on', 'boot', [['', 'what it has installed'], ['system', 'its own disk'],
        ['medium', 'the medium it was given']]),
      num('ssh port', 'ssh_port', 'blank = no ssh port', { style: { flex: '0 0 auto' } }),
    ] }),
    jsxs('label', { className: 'ah-namefield', children: [
      jsx('span', { className: 'ah-flabel', children: 'Forwarded ports' }),
      jsx('input', { className: 'ah-namein', type: 'text',
        placeholder: 'one per line — 8080:80, 5432:5432',
        value: String(f.ports || '').split('\n').join(', '), onChange: set('ports') }),
    ] }),
    jsxs('label', { className: 'ah-namefield', children: [
      jsx('span', { className: 'ah-flabel', children: 'Notes' }),
      jsx('input', { className: 'ah-namein', type: 'text', value: f.notes,
        placeholder: 'anything worth remembering about this machine', onChange: set('notes') }),
    ] }),
    bad ? jsx('div', { className: 'ah-why', children: bad }) : null,
    note ? jsx('div', { className: 'ah-note ah-clip', children: note }) : null,
    jsxs('div', { className: 'ah-row', children: [
      jsx(Button, { size: 'micro', disabled: !!busy, onClick: save,
        children: busy ? 'Saving…' : 'Save' }),
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy, onClick: onClose,
        children: 'Close' }),
      jsx('div', { className: 'ah-note', children: m.state === 'running'
        ? 'It is running: a change to the command line restarts it, the rest takes effect next time.'
        : 'Nothing here needs a restart until the next time it starts.' }),
    ] }),
  ] })
}

function MachineCard({ ctx, m, words, open, onOpen, menuOpen, onMenu, onAct, onExec, busy, out,
                       cmd, onCmd, need, onInstall, cons, consOpen, onConsole,
                       onWatch, onPopOut, onSettings, editOpen, fit, onSaved }) {
  const tail = (out || '').trim()
  const asks = needList(need)
  const screen = cons || {}
  const screenOn = !!consOpen
  return jsxs('div', { className: 'ah-card',
    // What the agent calls a machine is exactly what the panel calls it: the name.
    'data-athena-spot': 'machine:' + m.name, children: [
    jsxs('div', { className: 'ah-cartop', children: [
      jsx('div', { className: 'ah-icoph', children: '▣' }),
      jsxs('div', { style: { flex: '1 1 auto', minWidth: 0 }, children: [
        jsxs('h4', { children: [
          m.name,
          jsx('span', { className: 'ah-state' + (words.tone ? ' ' + words.tone : ''), children: words.chip }),
          m.attach ? jsx('span', { className: 'ah-badge own', children: 'your distro' }) : null,
        ] }),
        jsx('div', { className: 'ah-sub ah-clip', title: machineLine(m), children: machineLine(m) }),
      ] }),
      jsx('button', { className: 'ah-kebab' + (menuOpen ? ' ah-open' : ''),
        onClick: () => onMenu(menuOpen ? '' : m.name), title: 'More', children: '⋯' }),
    ] }),
    jsx('div', { className: 'ah-repo', children: m.last_op && m.last_op.op
      ? `${m.last_op.op} · ${String(m.last_op.at || '').replace('T', ' ')}`
      : 'nothing done with it yet' }),
    /* Something this machine needs before it can run. It sits above the buttons and
       is said before Start is pressed, not after: finding out from a click that does
       nothing is how a machine looks broken when it is only missing a dependency. */
    asks.length
      ? jsxs('div', { className: 'ah-detail', children: asks.map((n) => {
          const w = needWords(n)
          return jsxs('div', { className: 'ah-need' }, [
            jsx('div', { className: 'ah-needtitle', children: w.title }),
            w.detail ? jsx('div', { className: 'ah-note', children: w.detail }) : null,
            jsxs('div', { className: 'ah-row', children: [
              w.command ? jsx('code', { className: 'ah-code', children: w.command }) : null,
              jsx(Button, { size: 'micro', disabled: !!busy || !w.what,
                onClick: () => onInstall(w.what),
                children: busy === 'install' ? 'Setting it up…' : `Set up ${String(w.what).toUpperCase()}` }),
            ] }),
          ])
        }) })
      : null,
    menuOpen
      ? jsx('div', { className: 'ah-menu', children: [
          jsx('button', { className: 'ah-mi', onClick: () => { onMenu(''); onSettings(m.name) },
            children: 'Settings…' }),
          jsx('button', { className: 'ah-mi', onClick: () => { onMenu(''); onAct(m.name, 'snapshot') },
            children: 'Take a snapshot' }),
          jsx('button', { className: 'ah-mi', onClick: () => { onMenu(''); onAct(m.name, 'forget') },
            children: 'Forget it (keeps the distro)' }),
          jsx('button', { className: 'ah-mi ah-danger ah-misep', onClick: () => { onMenu(''); onAct(m.name, 'destroy') },
            children: 'Destroy it and its disk' }),
        ] })
      : null,
    open && editOpen
      ? jsx(MachineSettings, { ctx, m, fit, onClose: () => onSettings(''), onSaved })
      : null,
    open
      ? (m.provider === 'qemu'
        ? jsxs('div', { className: 'ah-detail', children: [
            // The screen is not drawn here. A guest is a computer, and a computer squeezed
            // into a card is a picture of one: the button opens it in a tab of its own, at
            // the size of the pane, which is also where typing and the window button live.
            jsxs('div', { className: 'ah-row', children: [
              jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
                onClick: () => onWatch(m.name),
                children: m.state === 'running' ? 'Watch its screen' : 'Its screen' }),
              jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
                onClick: () => onPopOut(m.name),
                children: busy === 'popout' ? 'Moving it…'
                  : (m.display === 'window' ? 'Bring it back into the panel'
                                            : 'Open in a new window') }),
            ] }),
            jsx('div', { className: 'ah-note', children: m.reach || (m.display === 'window'
              ? 'Its screen is a window of its own, opened when it starts.'
              : 'Its screen opens in a tab of its own — then it is yours: a click, a key, ' +
                'the wheel, all of it goes to the guest.') }),
            jsxs('div', { className: 'ah-row', children: [
              jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
                onClick: () => onConsole(screenOn ? '' : m.name),
                children: screenOn ? 'Hide the console' : 'Show the console' }),
              jsx('span', { className: 'ah-note', children: screenOn && screen.running
                ? 'live — its screen is updating' : 'what the guest itself has said' }),
            ] }),
            screenOn
              ? jsxs('div', { children: [
                  jsx('div', { className: 'ah-repo', children: screen.error
                    ? String(screen.error)
                    : screen.exists === false
                      ? 'nothing yet — it has not written a line to its console'
                      : `${screen.bytes || 0} bytes, last read ${String(screen.at || '').replace('T', ' ')}` }),
                  jsx('pre', { className: 'ah-pre ah-log', children: String(screen.text || '') || '…' }),
                  // QEMU's own words, kept apart from the guest's: an argument error or a
                  // crash is QEMU talking, not the guest, and it is the only thing that
                  // explains a machine that never got as far as speaking.
                  screen.errors
                    ? jsxs('div', { children: [
                        jsx('div', { className: 'ah-repo', children: 'qemu itself said:' }),
                        jsx('pre', { className: 'ah-pre ah-log', children: String(screen.errors) }),
                      ] })
                    : null,
                ] })
              : null,
            // A QEMU guest has no shell Athena can type into. The honest answer is
            // where it *is* reachable, not a command box that would refuse.
            jsx('div', { className: 'ah-note', children: machinePorts(m).length
              ? `Reachable at 127.0.0.1 — this guest does not share your localhost: ${machinePorts(m).join(', ')}.`
              : 'This guest has no forwarded port yet, so nothing in it is reachable from ' +
                'here. Add one, or give the machine an ssh port, and start it again.' }),
          ] })
        : jsxs('div', { className: 'ah-detail', children: [
            jsxs('div', { className: 'ah-row', children: [
              jsx(Field, { value: cmd, placeholder: 'run something inside it — ls /etc', onChange: onCmd,
                onSubmit: () => onExec(m.name) }),
              jsx(Button, { size: 'micro', disabled: !!busy, onClick: () => onExec(m.name),
                children: busy === 'run' ? 'Running…' : 'Run' }),
            ] }),
            tail ? jsx('pre', { className: 'ah-pre', children: tail }) : null,
            jsx('div', { className: 'ah-note', children: m.attach
              ? 'This machine is a distro you own — a command here can see your files.'
              : 'An Athena-owned distro: a command here cannot see your files.' }),
          ] }))
      : null,
    jsxs('div', { className: 'ah-foot', children: [
      words.verb
        ? jsx(Button, { size: 'micro', disabled: !!busy,
            onClick: () => onAct(m.name, words.what || 'start'),
            children: busy ? '…' : words.verb })
        : null,
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
        onClick: () => onOpen(open ? '' : m.name),
        children: open ? 'Hide' : (m.provider === 'qemu' ? 'How to reach it' : 'Run a command') }),
    ] }),
  ] })
}

/* Naming the machine is the first thing this tab asks anyone to do, and the field did not say
   so: a grey placeholder that read like a value already in it, no label, and a "Make it" that
   stayed dead without saying why — which is how the person who wrote this spent a minute working
   out what was missing. So the field is labelled, the hint is an instruction rather than a
   filled-in-looking value, the cursor starts in it, and the dead button carries its reason.
   Pure, and handed its callbacks rather than reaching for the tab's state, so the driver can
   draw every state of it. */
function newMachineRow(o) {
  const u = o || {}
  const name = String(u.name == null ? '' : u.name)
  const named = !!name.trim()
  const presets = u.presets || ['ubuntu-24.04', 'debian', 'arch', 'custom-iso']
  const field = (label, kids, extra) => jsxs('label',
    Object.assign({ className: 'ah-namefield' }, extra || {}, {
      children: [jsx('span', { className: 'ah-flabel', children: label })].concat(kids) }))
  return jsxs('div', { className: 'ah-row', children: [
    field('Name', [jsx('input', {
      className: 'ah-namein' + (named ? ' filled' : ''), type: 'text', value: name,
      autoFocus: true, placeholder: 'type a name — like linux-box',
      onChange: (e) => { if (u.onName) u.onName(e.target.value) },
      onKeyDown: (e) => {
        if (e.key === 'Enter' && named && !u.busy && u.onSubmit) u.onSubmit()
      },
    })]),
    field('Kind', [jsx('select', {
      className: 'ah-searchin', value: u.preset,
      onChange: (e) => { if (u.onPreset) u.onPreset(e.target.value) },
      children: presets.map((p) => jsx('option', { value: p, children: p }, p)),
    })], { style: { flex: '0 0 auto' } }),
    jsx(Button, { size: 'micro', disabled: !!u.busy || !named,
      onClick: () => { if (u.onSubmit) u.onSubmit() }, children: u.busy || 'Make it' }),
    named ? null : jsx('span', { className: 'ah-why', children: 'give it a name first' }),
  ] })
}

function MachinesTab({ ctx, st }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [note, setNote] = useState('')
  const [job, setJob] = useState(null)
  const [busy, setBusy] = useState('')
  const [open, setOpen] = useState('')
  const [menu, setMenu] = useState('')
  // The machine whose settings are open, by name. Kept apart from `open` because "how do I
  // reach it" and "change what it is" are different questions about the same card, and a form
  // that replaced the answer to the first would hide it when you ask the second.
  const [edit, setEdit] = useState('')
  const [showNew, setShowNew] = useState(false)
  // The three numbers start *blank* on purpose: blank means "size it for this
  // computer", which is a better first machine than one sized from a habit. The
  // placeholders show what the fit is, so blank is never a mystery.
  const [draft, setDraft] = useState({ name: '', preset: 'ubuntu-24.04', provider: '', imagePath: '',
    imageUrl: '', imageKind: '', os: 'linux', cpus: '', memoryMb: '', diskGb: '', ports: '',
    sshPort: '', attach: false, distro: '' })
  const [setup, setSetup] = useState(null)
  const [isoPick, setIsoPick] = useState('')
  const [isoOpen, setIsoOpen] = useState(false)
  const [isoBrowse, setIsoBrowse] = useState(false)
  const [isoDirDraft, setIsoDirDraft] = useState('')
  const [isoNote, setIsoNote] = useState('')
  const [cmd, setCmd] = useState('')
  const [out, setOut] = useState({})
  // Which image folders are open, whose console is showing, what is in it, and what a
  // click could not do because something is missing. All four are about the same thing:
  // answering "what happened" at the moment it happens instead of leaving it implied.
  const [openGroups, setOpenGroups] = useState({})
  const [consFor, setConsFor] = useState('')
  const [cons, setCons] = useState({})
  // The guest's screen, and which machine's is open. Read fresh on a timer rather
  // than streamed: these routes are plain HTTP, and a machine installing an OS needs
  // a picture every second or so, not sixty.
  // The screen's own state lives in the tab that shows it (MachineScreenTab). Nothing in
  // this tab draws a frame any more: a guest is a computer, not a picture in a card.
  const [need, setNeed] = useState([])
  const [forgotten, setForgotten] = useState([])
  const [cfg] = useSettings(ctx)
  const row = (cfg && cfg.apps_layout) === 'rows'

  const load = useCallback(async () => {
    try { setData(await ctx.rest('/machines', { timeoutMs: 20000 })); setErr('') }
    catch (e) { setErr(String((e && e.message) || e)) }
    // Machines that were forgotten rather than destroyed. Read after the list so a
    // failure here cannot take the machines away with it, and never assumed: the
    // leave-behind is on the disk, and this is Athena looking at it.
    try {
      const f = await ctx.rest('/machines/forgotten', { timeoutMs: 20000 })
      setForgotten((f && f.machines) || [])
    } catch (e) { /* the list of machines is still worth drawing */ }
  }, [ctx])
  useEffect(() => { load(); const i = setInterval(load, POLL_MS); return () => clearInterval(i) }, [load])

  // Setup is read once and after anything that could change it, not on the poll: it
  // carries the image folder listing and the size this computer can give a guest, and
  // both are read from the disk rather than from a cached answer.
  const loadSetup = useCallback(async () => {
    try { setSetup(await ctx.rest('/machines/setup', { timeoutMs: 20000 })) }
    catch (e) { /* the tab still draws machines without it */ }
  }, [ctx])
  useEffect(() => { loadSetup() }, [loadSetup])

  // Watch whatever this tab started. A job is watched by the tab that ran it, so
  // a provisioning run and a snapshot can never write over each other's line.
  const jobId = job && job.id
  useEffect(() => {
    if (!jobId) return
    let live = true
    const tick = async () => {
      try {
        const snap = await ctx.rest(`/jobs/${encodeURIComponent(jobId)}`, { timeoutMs: 20000 })
        if (!live) return
        setJob((j) => (j && j.id === jobId ? { ...j, snap } : j))
        if (snap && (snap.state === 'done' || snap.state === 'failed')) {
          setNote(`${job.label} — ${snap.state === 'done' ? 'done' : (snap.error || 'failed')}`)
          setJob(null)
          load()
        }
      } catch (e) { /* a read that fails is not a reason to stop drawing */ }
    }
    const i = setInterval(tick, 2000)
    tick()
    return () => { live = false; clearInterval(i) }
  }, [jobId, ctx, load, job && job.label])

  const act = useCallback(async (path, label, body) => {
    setBusy(label)
    try {
      const opts = body ? { method: 'POST', timeoutMs: 120000, body }
                        : { method: 'POST', timeoutMs: 120000 }
      const res = await ctx.rest(path, opts)
      const why = actionFailure(label, res)
      if (why) { setNote(why); await load(); return res }
      if (res && res.job) { setJob({ id: res.job, label, snap: null }) }
      else if (res && (res.note || res.elevated)) setNote(label + ' — ' + (res.note || 'started'))
      else if (res && res.manual) setNote(label + ' — ' + (res.note || 'needs doing by hand'))
      else setNote(label + ' — done')
      await load()
      return res
    } catch (e) { setNote(`${label}: ${e}`) } finally { setBusy('') }
  }, [ctx, load])

  const actBusy = useCallback(async (path, label, body) => {
    setBusy('verb')
    try { return await act(path, label, body) } finally { setBusy('') }
  }, [act])

  /* The console, watched by the tab that opened it: a QEMU guest has no shell to type
     into, so its serial output is the only live view of it the panel can give — the boot
     loader, the installer's first screen, a kernel panic. Read from the end, because the
     part worth seeing is the part that just happened. */
  useEffect(() => {
    if (!consFor) return
    let live = true
    const tick = async () => {
      try {
        const snap = await ctx.rest(`/machines/${encodeURIComponent(consFor)}/console`,
          { timeoutMs: 20000 })
        if (live) {
          setCons((c) => ({ ...c,
            [consFor]: { ...(snap || {}), at: new Date().toISOString() } }))
        }
      } catch (e) { /* a console read that fails is not a reason to stop drawing */ }
    }
    tick()
    const i = setInterval(tick, 2000)
    return () => { live = false; clearInterval(i) }
  }, [consFor, ctx])

  /* The screen, read while it is open on a machine that is running. Every frame is a PNG
     fetched through the plugin, which is the VNC client for the panel: a browser cannot
     open a raw socket to a guest, and these routes cannot upgrade to a websocket. 1.2s
     apart because a screen is worth watching, not worth streaming — and because a stopped
     machine has no screen to ask for. */
  const onConsole = useCallback((name) => {
    setConsFor((c) => (c === name ? '' : name))
  }, [])

  // Open a machine's screen where it belongs: its own tab, filling the pane.
  const setWatch = useCallback((name) => { watchMachine.set(name || null); tab.set('mscreen') }, [])

  /* Pop out, and back in: this changes where the machine's screen lives, which QEMU
     decides on its command line, so a running machine is restarted into the other one.
     The reply says which happened rather than leaving the click to look like it missed. */
  const onPopOut = useCallback(async (name) => {
    const row = (data && (data.machines || data.items || []).find((x) => x.name === name)) || {}
    const want = row.display === 'window' ? 'panel' : 'window'
    setBusy('popout')
    try {
      const res = await ctx.rest(`/machines/${encodeURIComponent(name)}/display`,
        { method: 'POST', timeoutMs: 120000, body: { display: want } })
      const why = actionFailure('Move the screen', res)
      if (why) { setNote(why); return res }
      setNote(`${name} — ${(res && res.note) || (want === 'window' ? 'popped out' : 'back in the panel')}`)
      await load()
      return res
    } catch (e) { setNote(`Move the screen: ${e}`) } finally { setBusy('') }
  }, [ctx, load, data])

  /* Start and stop are decided by what the machine *is*, before the click — not by
     trying a stop and starting only if it failed. That old order is why Start looked
     dead: pressing it on a stopped machine stopped it (already stopped), read the
     success as "done", and never started anything. A start that cannot happen says
     why, right here, and offers the thing that fixes it. */
  const onAct = useCallback(async (name, what) => {
    if (what === 'start' || what === 'stop') {
      const label = `${what === 'start' ? 'Start' : 'Stop'} ${name}`
      // Start puts the screen up: a machine you just started is something to look at, and
      // it opens in a tab of its own rather than as a thumbnail in the card it was started
      // from — the same way an app's output gets a tab. The console stays for when the
      // picture cannot tell the whole story.
      if (what === 'start') { setNeed([]); setConsFor(''); setWatch(name) }
      const res = await act(`/machines/${encodeURIComponent(name)}/${what}`, label)
      const asks = needList(res && res.needs_setup)
      if (asks.length) setNeed(asks)
      if (res && res.display_fallback) setNote(`${label} — ${res.display_fallback}`)
      else if (res && res.note) setNote(`${label} — ${res.note}`)
      else if (res && res.reach && res.ok) setNote(`${label} — running. ${res.reach}`)
      return
    }
    if (what === 'settings') { setMenu(''); setEdit(name); return }
    if (what === 'snapshot') return act(`/machines/${encodeURIComponent(name)}/snapshot`, `Snapshot ${name}`)
    if (what === 'forget') {
      if (!window.confirm(`Forget ${name}? Nothing is deleted: its disk stays where it is, ` +
        `Athena stops listing it, and you can bring it back from this tab.`)) return
      return act(`/machines/${encodeURIComponent(name)}/forget`, `Forget ${name}`)
    }
    if (what === 'destroy') {
      if (!window.confirm(`Destroy ${name} and its disk? This is the one that deletes: the ` +
        `distro on that disk is gone, and forgotten machines are not affected. Exports you ` +
        `already made are kept.`)) return
      return act(`/machines/${encodeURIComponent(name)}/destroy`, `Destroy ${name}`)
    }
  }, [act])

  /* The other half of forgetting: the folder comes back whole, and this is the only
     place that says how — a machine that is no longer in the list should not need a
     terminal to get back into it. */
  const onRestore = useCallback(async (name) => {
    setBusy('restore')
    try {
      const r = await ctx.rest(`/machines/forgotten/${encodeURIComponent(name)}/restore`,
        { method: 'POST', timeoutMs: 60000 })
      setNote(r && r.ok
        ? `${name} is back in the list, with the disk it was forgotten with.`
        : `Bring back ${name} — ${(r && (r.error || r.detail)) || 'it did not come back'}`)
      await load()
    } catch (e) { setNote(`Bring back ${name}: ${String((e && e.message) || e)}`) }
    finally { setBusy('') }
  }, [ctx, load])

  const onExec = useCallback(async (name) => {
    const command = String(cmd || '').trim()
    if (!command) return
    setBusy('run')
    try {
      const res = await ctx.rest(`/machines/${encodeURIComponent(name)}/exec`,
        { method: 'POST', timeoutMs: 300000, body: { command } })
      const line = res && res.ok
        ? `$ ${command}\n${String(res.output || '').trim() || '(no output)'}` +
          (res.truncated ? '\n… output was cut short' : '')
        : `$ ${command}\n${(res && (res.error || res.detail)) || 'it did not run'}`
      setOut((o) => ({ ...o, [name]: ((o[name] || '') + (o[name] ? '\n\n' : '') + line).slice(-4000) }))
      if (!res || !res.ok) setNote(`Run in ${name} — ${(res && res.error) || 'failed'}`)
    } catch (e) { setNote(`Run in ${name}: ${e}`) } finally { setBusy('') }
  }, [ctx, cmd])

  /* Setting a prerequisite up from the card that reported it. The ask is cleared on a
     success so the next poll draws a machine that can start, and the job it returns is
     watched like any other — installing QEMU is a download with a progress bar. */
  const onInstall = useCallback(async (what) => {
    const res = await act('/machines/setup/install', `Set up ${String(what).toUpperCase()}`, { what })
    if (res && res.ok) { setNeed([]); await loadSetup() }
    return res
  }, [act, loadSetup])

  /* Point the image folder somewhere — or make it, or go back to Athena's own. A
     path that does not exist yet comes back as that fact rather than as an empty
     folder made from a typo, which is why "Use this folder" and "Make it" are two
     buttons and not one clever one. */
  const setIsoDir = useCallback(async (folder, make) => {
    setBusy('iso-folder')
    try {
      const res = await ctx.rest('/machines/isos',
        { method: 'POST', timeoutMs: 20000, body: { dir: String(folder || ''), create: !!make } })
      if (res && res.ok) {
        setIsoNote(make ? `made ${res.dir}` : `reading images from ${res.dir}`)
        setIsoOpen(false)
        await loadSetup()
        await load()
      } else {
        setIsoNote(String((res && (res.error || res.detail)) || 'that folder did not take'))
      }
    } catch (e) { setIsoNote(String(e)) } finally { setBusy('') }
  }, [ctx, load, loadSetup])

  const onCreate = useCallback(async () => {
    // The provider follows the preset unless it was set by hand: `custom-iso` is the
    // preset that means "boot something of mine", and it is QEMU.
    const provider = draft.provider || (draft.preset === 'custom-iso' ? 'qemu' : 'wsl')
    const body = { name: String(draft.name || '').trim(), preset: draft.preset, provider,
      attach: !!draft.attach }
    if (draft.imagePath) body.image_path = String(draft.imagePath).trim()
    if (draft.imageUrl) body.image_url = String(draft.imageUrl).trim()
    if (draft.distro) body.distro = String(draft.distro).trim()
    if (provider === 'qemu') {
      // An ISO by default: the whole point of this half is install media. A disk
      // image is the other thing somebody can hand over, so it is a choice.
      body.image_kind = draft.imageKind || 'iso'
      body.os = String(draft.os || '')
      // Only what was actually typed is sent. Blank means `auto`, and the backend
      // sizes the machine from this computer's own cores, RAM and free space — the
      // numbers the form shows as placeholders, computed rather than assumed.
      const nums = { cpus: Number(draft.cpus), memory_mb: Number(draft.memoryMb),
        disk_gb: Number(draft.diskGb) }
      const given = Object.keys(nums).filter((k) => nums[k] > 0)
      if (!given.length) body.auto = true
      else given.forEach((k) => { body[k] = nums[k] })
      const ports = parsePorts(draft.ports)
      if (ports.length) body.ports = ports
      if (Number(draft.sshPort)) body.ssh_port = Number(draft.sshPort)
    }
    const res = await act('/machines', `Make ${body.name}`, body)
    if (res && res.ok) {
      await loadSetup()
      setShowNew(false)
      setDraft({ name: '', preset: 'ubuntu-24.04', provider: '', imagePath: '', imageUrl: '',
        imageKind: '', os: 'linux', cpus: '', memoryMb: '', diskGb: '', ports: '', sshPort: '',
        attach: false, distro: '' })
      setIsoPick('')
    }
  }, [act, draft, loadSetup])

  if (!data) return jsx('div', { className: 'ah-empty', children: err || 'Reading machines…' })

  const machines = data.machines || []
  const needs = data.needs_setup || []
  const unclaimed = data.unclaimed || []
  const qemu = (draft.provider || (draft.preset === 'custom-iso' ? 'qemu' : 'wsl')) === 'qemu'
  const progress = job && job.snap && job.snap.progress
  const lines = (job && job.snap && job.snap.log) || []
  // Images are listed by folder, newest first, and a folder is a line you open rather
  // than a wall you scroll: the listing of a folder somebody has been filling for
  // years is exactly what "expand" is for. `everyOpen` is what the one button says.
  const isoGroupsList = isoGroupsShown(setup && setup.isos, openGroups)
  const everyOpen = isoGroupsList.length > 0 && isoGroupsList.every((g) => g.open)

  return jsxs('div', { children: [
    jsx('div', { className: 'ah-sub', style: { marginBottom: 8 }, children:
      'A machine is a real OS Athena can start, stop and run commands in — for anything that ' +
      'will not install on this computer. Athena\u2019s own, so nothing in it can reach your files.' }),
    jsxs('div', { className: 'ah-filtrow', children: [
      jsx('span', { className: 'ah-sub', children: isoWords(setup && setup.isos) }),
      jsx('span', { className: 'ah-grow' }),
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy, onClick: () => {
        setIsoBrowse(!isoBrowse)
        setIsoOpen(false)
        setIsoDirDraft(String((setup && setup.isos && setup.isos.dir) || ''))
      }, children: isoBrowse ? 'Done' : 'Change folder\u2026' }),
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy, onClick: () => {
        setIsoOpen(!isoOpen)
        setIsoBrowse(false)
        setIsoDirDraft(String((setup && setup.isos && setup.isos.dir) || ''))
      }, children: isoOpen ? 'Cancel' : 'Type a path' }),
    ] }),
    isoBrowse
      ? jsx(FolderPicker, { ctx, busy, start: String((setup && setup.isos && setup.isos.dir) || ''),
          onUse: (folder) => { setIsoBrowse(false); setIsoDir(folder, false) },
          onClose: () => setIsoBrowse(false) })
      : null,
    isoOpen
      ? jsxs('div', { className: 'ah-notice', children: [
          jsx('div', { className: 'ah-sub', children: 'Where the machine images live. Athena ' +
            'reads this one folder \u2014 it does not go looking through your drives \u2014 and it ' +
            'starts nothing until you start it, so nothing downloads until then.' }),
          jsxs('div', { className: 'ah-row', children: [
            jsx(Field, { value: isoDirDraft,
              placeholder: String((setup && setup.isos && setup.isos.inbox) || 'D:/isos'),
              onChange: setIsoDirDraft }),
            jsx(Button, { size: 'micro', disabled: !!busy, onClick: () => setIsoDir(isoDirDraft, false),
              children: busy === 'iso-folder' ? 'Reading…' : 'Use this folder' }),
            jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
              onClick: () => setIsoDir(isoDirDraft, true), children: 'Make it' }),
            jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
              onClick: () => setIsoDir('', false), children: 'Athena\u2019s own folder' }),
            jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
              onClick: loadSetup, children: 'Re-read' }),
          ] }),
          isoNote ? jsx('div', { className: 'ah-note', children: isoNote }) : null,
        ] })
      : null,
    /* The images that are actually in the folder, as a list you can pick from: the menu
       in the form is for choosing quickly, this is for seeing what is there. Picking one
       fills the form the same way the menu does, so there is one rule for both. */
    ((setup && setup.isos && setup.isos.isos) || []).length
      ? jsxs('div', { className: 'ah-notice', children: [
          jsxs('div', { className: 'ah-filtrow', children: [
            jsx('span', { className: 'ah-sub', children: 'Pick an image to build a machine from ' +
              '\u2014 what is in that folder, subfolders included. A folder opens when you click it.' }),
            jsx('span', { className: 'ah-grow' }),
            jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
              onClick: () => setOpenGroups(groupMap(setup && setup.isos, !everyOpen)),
              children: everyOpen ? 'Minimize all' : 'Expand all' }),
          ] }),
          isoGroupsList.map((g) => jsxs('div', { children: [
            jsx('button', { className: 'ah-group' + (g.open ? ' ah-open' : ''),
              onClick: () => setOpenGroups((o) => ({ ...o, [g.rel]: !g.open })),
              children: `${g.open ? '\u25be' : '\u25b8'} ${g.label} (${g.count})` }),
            // Nothing is held back in a folder you opened yourself: the whole point of
            // opening it is to see what is in it, and a "…and 6 more" under a click that
            // asked for exactly that is a worse answer than a long list.
            g.open
              ? (g.items.length
                ? g.items.map((i) => jsxs('div', { className: 'ah-filtrow ah-ingroup', children: [
                    jsx('span', { style: { flex: '1 1 auto', wordBreak: 'break-all' },
                      children: isoLabel(i) }),
                    jsx(Button, { size: 'micro',
                      variant: isoPick === String(i.path) ? undefined : 'ghost',
                      onClick: () => {
                        setDraft((d) => ({ ...d, preset: 'custom-iso', provider: 'qemu', ...isoPatch(i) }))
                        setIsoPick(String(i.path || ''))
                        setShowNew(true)
                      }, children: isoPick === String(i.path) ? 'chosen' : 'New machine' }),
                  ] }, 'iso' + String(i.path)))
                : jsx('div', { className: 'ah-sub ah-ingroup', children: 'nothing in this one' }))
              : null,
          ] }, 'g' + (g.rel || 'root'))),
        ] })
      : null,
    needs.map((n, i) => jsx(SetupCard, { need: n, busy: busy, onInstall,
      onUseDistro: (name) => setDraft((d) => ({ ...d, attach: true, distro: name, name: d.name || name })) }, 'need' + i)),
    // What a click just reported missing, when the row itself does not say it — an
    // attached machine whose distro Athena cannot see, say. Anything the tab already
    // shows above is dropped rather than drawn twice.
    need.filter((n) => !needs.some((x) => JSON.stringify(x) === JSON.stringify(n)))
      .map((n, i) => jsx(SetupCard, { need: n, busy: busy, onInstall,
        onUseDistro: (name) => setDraft((d) => ({ ...d, attach: true, distro: name, name: d.name || name })) },
        'ask' + i)),
    note ? jsx('div', { className: 'ah-notice', children: note }) : null,
    job ? jsxs('div', { className: 'ah-runsec', children: [
      jsx('div', { className: 'ah-runhead', children: job.label }),
      progress ? jsx(JobProgress, { progress }) : null,
      lines.length ? jsx(AutoTerm, { className: 'ah-term',
        text: lines.slice(-8).join('\n') }) : null,
    ] }) : null,
    jsxs('div', { className: 'ah-filtrow', children: [
      jsx('span', { className: 'ah-sub', children: machines.length
        ? `${machines.length} machine${machines.length === 1 ? '' : 's'}`
        : 'no machines yet' }),
      jsx('span', { className: 'ah-grow' }),
      jsx(Button, { size: 'micro', variant: showNew ? 'ghost' : undefined,
        onClick: () => setShowNew(!showNew), children: showNew ? 'Cancel' : 'New machine' }),
    ] }),
    showNew
      ? jsxs('div', { className: 'ah-notice', children: [
          jsx(newMachineRow, { name: draft.name, preset: draft.preset, busy: busy,
            onName: (v) => setDraft((d) => ({ ...d, name: v })),
            onPreset: (v) => setDraft((d) => ({ ...d, preset: v })),
            onSubmit: onCreate }),
          qemu
            ? jsxs('div', { className: 'ah-row', children: [
                jsx('select', { className: 'ah-searchin', style: { flex: '1 1 240px' }, value: isoPick,
                  onChange: (e) => {
                    const v = e.target.value
                    setIsoPick(v)
                    if (v === '__other__') setDraft((d) => ({ ...d, imagePath: '' }))
                    else {
                      // Picking from the folder fills the path, the kind and (from the
                      // name alone) a suggested guest OS — all three visible in the form,
                      // so a suggestion that is wrong is a field somebody changes.
                      const item = (((setup && setup.isos && setup.isos.isos) || [])
                        .find((i) => String(i.path) === v))
                      setDraft((d) => ({ ...d, ...isoPatch(item) }))
                    }
                  },
                  children: isoOptions(setup && setup.isos).map((o) =>
                    jsx('option', { value: o.value, children: o.label }, o.value)) }),
                jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
                  onClick: loadSetup, children: 'Re-read folder' }),
              ] })
            : null,
          jsxs('div', { className: 'ah-row', children: [
            jsx(Field, { value: draft.imagePath, placeholder: qemu ? 'an .iso or disk image already here — D:/iso/bazzite.iso' : 'or an image you already have — /path/to/rootfs.tar.gz',
              onChange: (v) => setDraft((d) => ({ ...d, imagePath: v })) }),
          ] }),
          qemu
            ? jsxs('div', { children: [
                jsx('div', { className: 'ah-row', children: jsx(Field, { value: draft.imageUrl,
                  placeholder: 'or a URL to fetch once — https://…/Omarchy.iso',
                  onChange: (v) => setDraft((d) => ({ ...d, imageUrl: v })) }) }),
                jsxs('div', { className: 'ah-row', children: [
                  jsx('select', { className: 'ah-searchin', style: { flex: '0 0 auto' },
                    value: draft.imageKind || 'iso',
                    onChange: (e) => setDraft((d) => ({ ...d, imageKind: e.target.value })),
                    children: [['iso', 'an installer (.iso)'], ['disk', 'a disk image (.qcow2/vhdx to boot as it is)']]
                      .map(([v, label]) => jsx('option', { value: v, children: label }, v)) }),
                  jsx('select', { className: 'ah-searchin', style: { flex: '0 0 auto' },
                    value: draft.os,
                    onChange: (e) => setDraft((d) => ({ ...d, os: e.target.value })),
                    children: [['linux', 'Linux guest'], ['windows', 'Windows guest']]
                      .map(([v, label]) => jsx('option', { value: v, children: label }, v)) }),
                  jsx(Field, { value: draft.cpus,
                    placeholder: String((setup && setup.fit && setup.fit.cpus) || 'cores'),
                    style: { flex: '0 0 70px' },
                    onChange: (v) => setDraft((d) => ({ ...d, cpus: v })) }),
                  jsx(Field, { value: draft.memoryMb,
                    placeholder: String((setup && setup.fit && setup.fit.memory_mb) || 'MB'),
                    style: { flex: '0 0 80px' },
                    onChange: (v) => setDraft((d) => ({ ...d, memoryMb: v })) }),
                  jsx(Field, { value: draft.diskGb,
                    placeholder: String((setup && setup.fit && setup.fit.disk_gb) || 'GB'),
                    style: { flex: '0 0 70px' },
                    onChange: (v) => setDraft((d) => ({ ...d, diskGb: v })) }),
                ] }),
                jsx('div', { className: 'ah-note', children: fitWords(setup && setup.fit) +
                  ' \u2014 leave the three numbers blank to take it.' }),
                jsxs('div', { className: 'ah-row', children: [
                  jsx(Field, { value: draft.ports, placeholder: 'ports to reach it — 8080:80, 5432:5432',
                    onChange: (v) => setDraft((d) => ({ ...d, ports: v })) }),
                  jsx(Field, { value: draft.sshPort, placeholder: 'ssh 2222', style: { flex: '0 0 110px' },
                    onChange: (v) => setDraft((d) => ({ ...d, sshPort: v })) }),
                ] }),
                jsx('div', { className: 'ah-note', children: 'A Windows guest gets IDE disks and ' +
                  'an e1000 NIC automatically — an installer has no virtio driver and reports a ' +
                  'machine with no hard drive. This guest does not share your localhost, so a ' +
                  'port you want to open has to be listed here.' }),
              ] })
            : null,
          jsxs('label', { className: 'ah-row ah-pick', children: [
            jsx('input', { type: 'checkbox', checked: !!draft.attach,
              onChange: (e) => setDraft((d) => ({ ...d, attach: e.target.checked })) }),
            jsx('span', { className: 'ah-sub', children: 'use a distro I already have instead (a command ' +
              'in it can see my files)' }),
          ] }),
          draft.attach
            ? jsx('div', { className: 'ah-row', children: jsx(Field, { value: draft.distro,
                placeholder: 'which distro — Ubuntu', onChange: (v) => setDraft((d) => ({ ...d, distro: v })) }) })
            : null,
          jsx('div', { className: 'ah-note', children: qemu
            ? 'The image is fetched on first start, into the machine\u2019s own folder — nothing ' +
              'downloads until you start it. QEMU itself is a system package, so Athena asks ' +
              'before installing it.'
            : 'A preset image is fetched on first start — about 340 MB for Ubuntu. Point at your ' +
              'own tarball to run Debian, Arch, or anything else that ships one.' }),
        ] })
      : null,
    machines.length
      ? jsx('div', { className: 'ah-grid' + (row ? ' ah-rows' : ''), children: machines.map((m) =>
          jsx(MachineCard, { ctx, m, words: machineWords(m), busy, open: open === m.name,
            menuOpen: menu === m.name, out: out[m.name], cmd: open === m.name ? cmd : '', onCmd: setCmd,
            onOpen: setOpen, onMenu: setMenu, onAct, onExec,
            // What this machine is missing, so a card can say it before Start is pressed
            // rather than after; and its console, so pressing Start shows the guest.
            need: (m.needs_setup && !(m.needs_setup instanceof Array)) ? [m.needs_setup] : m.needs_setup,
            onInstall, cons: cons[m.name], consOpen: consFor === m.name, onConsole,
            // And its screen: opened by Start, and openable by hand on any machine that is
            // already running. Pop out moves it into a window and back.
            onWatch: setWatch, onPopOut,
        // Changing what a machine is: the same rules as an agent's edit, typed instead of
        // spoken, and the panel only reports what the backend said came back.
        onSettings: setEdit, editOpen: edit === m.name, fit: (setup && setup.fit) || null,
        onSaved: load }, m.name)) })
      : jsx('div', { className: 'ah-empty', children:
          'No machines yet. One is enough to answer questions like \u201cdoes this install on ' +
          'Linux\u201d, and a second answers \u201cwhat about Arch\u201d.' }),
    forgotten.length
      ? jsxs('div', { children: [
          jsx('div', { className: 'ah-runhead', style: { marginTop: 10 }, children:
            'Machines Athena has forgotten (what they had is kept)' }),
          jsx('div', { className: 'ah-row', children: forgotten.map((f) =>
            jsxs('div', { className: 'ah-row', children: [
              jsxs('span', { className: 'ah-sub', children: [f.name + ' · keeps ' + f.keeps +
                (f.disk_bytes ? ' (' + (f.disk_bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GiB)'
                              : '')] }),
              jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
                onClick: () => onRestore(f.name), children: 'Bring it back' }),
            ] }, f.name)) }),
          jsx('div', { className: 'ah-note', children: 'Forgetting moves a machine out of this ' +
            'list and leaves its disk alone; destroying deletes the disk. Bringing one back ' +
            'moves the whole folder, disk included, and everything about the machine is as it ' +
            'was.' }),
        ] })
      : null,
    unclaimed.length
      ? jsxs('div', { children: [
          jsx('div', { className: 'ah-runhead', style: { marginTop: 10 }, children:
            'Distros on this computer that Athena is not managing' }),
          jsx('div', { className: 'ah-row', children: unclaimed.map((d) =>
            jsxs('div', { className: 'ah-row', children: [
              jsxs('span', { className: 'ah-sub', children: [`${d.name} · ${d.state}`] }),
              jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => {
                setShowNew(true)
                setDraft((s) => ({ ...s, attach: true, distro: d.name, name: s.name || d.name }))
              }, children: 'Use it' }),
            ] }, d.name)) }),
          jsx('div', { className: 'ah-note', children: 'Attaching means commands Athena runs can ' +
            'see the files in that distro. Athena will not stop or delete it.' }),
        ] })
      : null,
  ] })
}

function shellTabs(terminal, job, machine) {
  const tabs = [['apps', 'Apps'], ['store', 'Store'], ['files', 'Files'], ['machines', 'Machines'], ['viewer', 'App'], ['runs', 'Runs'], ['settings', 'Settings']]
  // In front of Runs -- history -- and Settings, so it sits with the app views it
  // belongs to. Found by name and not by index: the merge rewrites this array, so
  // an index chosen here would land somewhere else in the merged bar.
  const at = () => Math.max(1, tabs.findIndex(([k]) => k === 'runs' || k === 'settings'))
  // A machine you are watching gets a tab of its own, named after it -- the same way an
  // app's output gets one. It is put in first so a screen you just opened is beside the
  // workbench it came from rather than after Settings.
  if (machine) tabs.splice(at(), 0, ['mscreen', String(machine)])
  // `terminal` is the app someone asked to watch; `job` is an action with output
  // and no single app behind it — updating the whole shelf is one. Both mean
  // there is output to watch, so both put the tab in the bar.
  if (terminal || job) tabs.splice(at(), 0, ['terminal', 'Terminal'])
  return tabs
}

/* What is happening right now: the steps of whatever action is running, above
   whatever the app is printing. A terminal rather than a log, because "log" reads
   as history and history is the Runs tab — this is the screen you watch while an
   install clones, downloads or builds. Nothing here is read once: it is read on a
   timer and followed, and following stops when you scroll up to read what went
   past. */
function TerminalTab({ ctx }) {
  const app = useValue(termApp)
  const job = useValue(activeJob)
  const [text, setText] = useState('')
  const [steps, setSteps] = useState('')
  // The job's own snapshot, kept so the header can say how long it has been
  // running and how long since it last said anything — rather than the flat
  // "working…" that is true of a hung job too.
  const [pace, setPace] = useState(null)
  const [err, setErr] = useState('')
  const [follow, setFollow] = useState(true)
  // Where the app is serving, when it is. The terminal is the one place that
  // answers "is it up yet", and the address is the answer.
  const [live, setLive] = useState('')
  const box = useRef(null)
  // A job for one app belongs to that app's screen. A job for the whole shelf
  // has no app to point at, and its steps are the entire story.
  // A job with no id is a job that has been asked for and not yet named — the request is
  // still in flight. It used to be a job all the same, so the tab polled `/jobs/null` for as
  // long as the request took: a phantom job, read forever, belonging to nothing.
  const mine = job && job.id && (!app || job.app === app) ? job : null
  const starting = job && !job.id ? String(job.label || 'Working') : ''

  const load = useCallback(async () => {
    if (!app) return
    try {
      const r = await ctx.rest(`/apps/${encodeURIComponent(app)}/terminal`, { timeoutMs: 30000 })
      setText(typeof r.output === 'string' ? r.output : JSON.stringify(r, null, 2))
      setLive(r.url || '')
      setErr('')
    } catch (e) { setErr(String(e)) }
  }, [ctx, app])

  // What the action itself is doing, above what the app is printing. An install
  // spends its first minute cloning before the app prints anything, and "nothing
  // is happening" during that minute is exactly what this tab is for.
  const loadJob = useCallback(async () => {
    if (!mine) { setSteps(''); setPace(null); return }
    try {
      const j = await ctx.rest(`/jobs/${encodeURIComponent(mine.id)}`, { timeoutMs: 30000 })
      setSteps(j.tail || '')
      setPace(j)
    } catch (e) { /* job gone; the app's own log still stands on its own */ setPace(null) }
  }, [ctx, mine && mine.id])

  useEffect(() => { load() }, [load])
  useEffect(() => { loadJob() }, [loadJob])

  // A log you have to refresh by hand is not a log. An install or a launch writes
  // to this file continuously, and reading it on a timer is the only way to watch
  // it happen.
  useEffect(() => {
    if (!app && !mine) return undefined
    const t = setInterval(() => { load(); loadJob() }, 2000)
    return () => clearInterval(t)
  }, [app, mine && mine.id, load, loadJob])

  // Stay at the end of the output. Scrolling up means someone wants to read what
  // went past, so following stops until they return to the bottom.
  useEffect(() => {
    const el = box.current
    if (el && follow) el.scrollTop = el.scrollHeight
  }, [text, steps, follow])

  if (!app && !mine && !starting) {
    return jsx('div', { className: 'ah-empty',
      children: 'Open an app in the Apps tab and press its Terminal button.' })
  }
  const body = (steps ? steps.replace(/\s+$/, '') + '\n\n' : '') + (text || '(no output yet)')
  return jsxs('div', { children: [
    jsxs('div', { className: 'ah-row', style: { marginBottom: 8 }, children: [
      jsx('div', { className: 'ah-title', children: app || 'Every app' }),
      mine ? (() => { const p = jobPace(pace)
          return p.text
            ? jsx('span', { className: 'ah-sub' + (p.tone ? ' ah-pace-' + p.tone : ''),
                            title: p.title, children: p.text })
            : jsx('span', { className: 'ah-sub', children: 'working…' }) })()
           : (starting
               ? jsx('span', { className: 'ah-sub', children: `${starting} — starting…` })
               : (live ? jsx('span', { className: 'ah-sub', children: live }) : null)),
      jsx('div', { className: 'ah-grow' }),
      jsx(Button, { size: 'micro', variant: 'ghost',
        onClick: () => {
          setFollow(true)
          const el = box.current
          if (el) el.scrollTop = el.scrollHeight
        },
        children: follow ? 'Following' : 'Follow' }),
      jsx(Button, { size: 'micro', onClick: load, children: 'Refresh' }),
      // Closing the terminal closes its tab, so leaving is the only sensible
      // place to land rather than on a tab that no longer exists.
      jsx(Button, { size: 'micro', variant: 'ghost',
        onClick: () => { termApp.set(null); activeJob.set(null); tab.set('apps') },
        children: 'Close' }),
    ] }),
    err ? jsx('div', { className: 'ah-warn', children: err }) : null,
    jsx('div', {
      className: 'ah-pre',
      ref: box,
      onScroll: (e) => {
        const el = e.currentTarget
        setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
      },
      children: body,
    }),
  ] })
}

/* -------------------------------------------------------------- disk saver */

// Apps repeat each other — the same model downloaded by two apps, the same
// native binary built into two installs — and that repetition is the main way an
// app collection quietly eats a drive. The engine can find files that are
// byte-for-byte identical and replace all but one with a hardlink, so every app
// still sees ordinary files while the bytes exist once.
//
// This drives the engine's own implementation instead of reimplementing it. That
// is the point: the safety rules — never crossing a filesystem, never touching a
// file a running app holds, re-verifying content immediately before replacing
// anything, only ever freeing a copy whose link count reached one — are exactly
// the part not worth getting wrong by hand.
function DiskSaver({ ctx }) {
  const [summary, setSummary] = useState(null)
  const [reason, setReason] = useState('')
  const [term, setTerm] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      const d = await ctx.rest('/disk', { timeoutMs: 180000 })
      if (d && d.ok) { setSummary(d.summary); setReason('') }
      else setReason((d && d.reason) || 'unavailable')
    } catch (e) { setReason(String(e)) }
  }, [ctx])

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load])

  const run = async (path, label) => {
    setBusy(path)
    setTerm('')
    setMsg(label + '…')
    try {
      const r = await ctx.rest(path, { method: 'POST', timeoutMs: 120000 })
      if (!r || !r.ok) { setMsg('Failed: ' + ((r && r.error) || 'unknown')); return }
      const out = await awaitJob(ctx, r.job, { onTick: (j) => setTerm(j.tail || '') })
      setTerm((out.job && out.job.tail) || '')
      setMsg(out.ok ? 'Finished.' : ('Failed: ' + (out.error || 'unknown')))
      await load()
    } catch (e) { setMsg('Failed: ' + e) } finally { setBusy('') }
  }

  const s = summary || {}
  const scanning = busy === '/disk/scan' || !!s.scanning

  /* Two of these buttons do something to files that one click should not be enough for, and
     both of them are about the numbers above: "Free unshared" deletes stored copies, and
     "Restore independent copies" spends the space this whole section exists to save. Asking
     first is not ceremony, it is the difference between a button that reports a number and a
     button that acts on it — and it says the number out loud, because a confirmation that does
     not carry the figure is a confirmation the reader has to guess at. */
  const confirmFor = (path, label) => {
    const reclaim = s.pending_bytes ? fmtBytes(s.pending_bytes)
      : (s.reclaimable ? fmtBytes(s.reclaimable) : 'some space')
    const shared = s.saved_by_sharing ? fmtBytes(s.saved_by_sharing) : 'the space it freed'
    if (path === '/disk/free') {
      return window.confirm(`${label}?\n\nThis deletes stored copies that nothing links to any `
        + `more — up to ${reclaim} goes back to the drive. The files every app sees are left `
        + `alone, because nothing points at them any more.`)
    }
    if (path === '/disk/separate') {
      return window.confirm(`${label}?\n\nEvery shared file gets its own copy back: about `
        + `${shared} is used again, in exchange for each app owning its files outright. `
        + `Reclaim can undo it.`)
    }
    return true
  }
  const runSure = (path, label) => { if (confirmFor(path, label)) run(path, label) }

  return jsxs('div', { children: [
    jsx('div', { className: 'ah-secthead', children: 'Reclaim disk space' }),
    jsx('div', { className: 'ah-sub', style: { marginBottom: 6 }, children:
      'Apps repeat each other: the same model downloaded twice, the same binary '
      + 'built into two installs. Athena finds files that are byte-for-byte '
      + 'identical and keeps one copy, linking the rest to it — every app still '
      + 'sees ordinary files. Nothing changes until you say so.' }),

    (reason && !summary)
      ? jsx('div', { className: 'ah-sub', children: 'Not available yet: ' + reason })
      : jsxs('div', { children: [
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'Last scan' }),
            jsx('span', { children: scanning ? 'running…'
              : (s.phase || s.last_outcome || 'never') })] }),
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'Files looked at' }),
            jsx('span', { children: s.files ? s.files.toLocaleString() : '—' })] }),
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'On disk' }),
            jsx('span', { children: s.bytes_on_disk ? fmtBytes(s.bytes_on_disk) : '—' })] }),
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'Already shared' }),
            jsx('span', { className: s.saved_by_sharing ? 'ah-ok' : '',
              children: s.saved_by_sharing ? fmtBytes(s.saved_by_sharing) : 'none yet' })] }),
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'Ready to reclaim' }),
            jsx('span', { className: s.pending_bytes ? 'ah-ok' : '',
              children: s.pending_bytes ? fmtBytes(s.pending_bytes)
                : (s.reclaimable ? fmtBytes(s.reclaimable) : 'nothing found') })] }),
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'Minimum file size' }),
            jsx('span', { children: s.candidate_min_bytes ? fmtBytes(s.candidate_min_bytes) : '—' })] }),
        ] }),

    jsxs('div', { className: 'ah-row', style: { marginTop: 10 }, children: [
      jsx(Button, { size: 'micro', disabled: !!busy,
        onClick: () => run('/disk/scan', 'Looking for duplicate files'),
        children: scanning ? 'Scanning…' : 'Scan' }),
      jsx(Button, { size: 'micro', disabled: !!busy,
        onClick: () => run('/disk/dedupe', 'Replacing duplicates with links'),
        children: busy === '/disk/dedupe' ? 'Reclaiming…' : 'Reclaim space' }),
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
        onClick: () => runSure('/disk/free', 'Free unshared copies'),
        children: busy === '/disk/free' ? '…' : 'Free unshared' }),
      // The reverse of Reclaim. Deliberately ghost/secondary — it is the one
      // control here that spends disk space instead of saving it — and labelled
      // so, because a button that quietly undoes the saving reads as a bug.
      jsx(Tip, { label: 'Gives every shared file its own copy back. This uses '
        + 'MORE disk space — it undoes what Reclaim freed — in exchange for each '
        + 'app owning its files outright.', children:
        jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!busy,
          onClick: () => runSure('/disk/separate', 'Restore independent copies'),
          children: busy === '/disk/separate' ? 'Restoring…' : 'Restore independent copies' }) }),
    ] }),
    jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children:
      'Restore independent copies is the reverse of Reclaim: every file that was '
      + 'linked to a shared copy gets its own copy back. It costs disk space — the '
      + 'opposite of reclaiming — and buys independence, so nothing one app does to '
      + 'a file can reach another.' }),

    term ? jsx(AutoTerm, { text: term }) : null,
    msg ? jsx('div', { className: 'ah-sub', children: msg }) : null,
  ] })
}

/* ------------------------------------------------------- shared store */

function SharedStore({ ctx }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [term, setTerm] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const d = await ctx.rest('/shared', { timeoutMs: 180000 })
      if (d && !d.error) { setData(d); setErr('') }
      else setErr((d && d.error) || 'unavailable')
    } catch (e) { setErr(String(e)) }
  }, [ctx])

  useEffect(() => {
    load()
    const t = setInterval(load, 20000)
    return () => clearInterval(t)
  }, [load])

  const unify = async () => {
    setBusy(true); setTerm(''); setMsg('Pointing every app at one copy…')
    try {
      const r = await ctx.rest('/shared/unify', { method: 'POST', timeoutMs: 120000 })
      if (!r || !r.ok) { setMsg('Failed: ' + ((r && r.error) || 'unknown')); return }
      const out = await awaitJob(ctx, r.job, { onTick: (j) => setTerm(j.tail || '') })
      setTerm((out.job && out.job.tail) || '')
      const moved = (out.result && out.result.moved_bytes) || 0
      setMsg(out.ok
        ? (moved ? ('Folded ' + fmtBytes(moved) + ' into the shared copy.')
                 : 'Already sharing everything — nothing to move.')
        : ('Failed: ' + (out.error || 'unknown')))
      await load()
    } catch (e) { setMsg('Failed: ' + e) } finally { setBusy(false) }
  }

  const d = data || {}
  const privateCount = (d.app_count || 0) - (d.unified_count || 0)

  return jsxs('div', { children: [
    jsx('div', { className: 'ah-secthead', children: 'One copy of everything' }),
    jsx('div', { className: 'ah-sub', style: { marginBottom: 6 }, children:
      'Apps each bring their own idea of where models belong, so the same model '
      + 'gets downloaded once per app. Athena points them all at one store — '
      + 'models, torch bundles, pip and uv caches — and moves anything already '
      + 'downloaded into it. Settings and state are left alone: two apps sharing '
      + 'one config file is worse than a duplicate.' }),

    err && !data
      ? jsx('div', { className: 'ah-sub', children: 'Not available yet: ' + err })
      : jsxs('div', { children: [
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'Apps sharing' }),
            jsx('span', { className: privateCount ? '' : 'ah-ok',
              children: (d.unified_count || 0) + ' of ' + (d.app_count || 0) })] }),
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'Kept separately' }),
            jsx('span', { className: d.private_bytes ? 'ah-warn' : '',
              children: d.private_bytes ? fmtBytes(d.private_bytes) : 'nothing' })] }),
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'Models on disk' }),
            jsx('span', { children: d.shared_bytes ? fmtBytes(d.shared_bytes) : '—' })] }),
          jsx('div', { className: 'ah-kv', children: [jsx('span', { children: 'Shared location' }),
            jsx('span', { className: 'ah-sub', style: { maxWidth: '60%', overflowWrap: 'anywhere' },
              children: d.shared_root || '—' })] }),
        ] }),

    (d.apps || []).filter(a => !a.unified).length
      ? jsx('div', { style: { marginTop: 8 }, children: (d.apps || []).filter(a => !a.unified)
          .map(a => jsx('div', { key: a.folder, className: 'ah-sub', children:
            a.app + ' keeps ' + fmtBytes(a.private_bytes) + ' to itself ('
            + a.private.map(p => p.key).join(', ') + ')' })) })
      : null,

    jsxs('div', { className: 'ah-row', style: { marginTop: 10 }, children: [
      jsx(Button, { size: 'micro', disabled: busy, onClick: unify,
        children: busy ? 'Sharing…' : 'Share now' }),
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: busy, onClick: load,
        children: 'Refresh' }),
    ] }),

    term ? jsx(AutoTerm, { text: term }) : null,
    msg ? jsx('div', { className: 'ah-sub', children: msg }) : null,
  ] })
}

/* --------------------------------------------------------- weight shelf */

// What this disk is full of, and what it still lacks. Read from the machine's
// end: every library with what it holds and what it costs, what no app reads any
// more, and which repositories are cached (cached is not waste — asking for one
// again never touches the network). The pull row is the same fact turned into an
// action: name a repository and the file lands in the library the chosen app
// already reads, so an app that could not generate can.
/*
  What is asking something of you, at the top of the System window — the window
  someone opens when something is not working, so the first thing in it should be
  the answer to that question rather than a disk list.

  It renders `GET /report`, which is a *reading*: every fact in it is already known
  (failing checks, the cached update answers, the weight inventory, the wait queue),
  so re-reading it costs a directory walk and never touches the network. That is why
  there is a Re-read button here at all.

  The rows say what to do in words rather than carrying the buttons that do it. The
  buttons that act live on the app's own card, one pane away, where the app they act
  on is named and the outcome is visible; a second set here would be a second place
  for the same decision to be made differently. "Show it" is therefore the whole
  action this list offers — routing, not doing.
*/
function NeedsYou({ ctx }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setBusy(true)
    try {
      const r = await ctx.rest('/report', { timeoutMs: 120000 })
      setData(r || {})
      setErr('')
    } catch (e) {
      setErr(String((e && e.message) || e))
    } finally { setBusy(false) }
  }, [ctx])

  useEffect(() => { load() }, [load])

  if (data === null) {
    return jsxs('div', { children: [
      jsx('div', { className: 'ah-secthead', children: 'What needs you' }),
      jsx('div', { className: 'ah-sub', children: err
        ? 'Could not read it: ' + err
        : 'Reading what this machine already knows…' }),
    ] })
  }

  return needsPanel(data, { onReread: load, busy })
}

/*
  The report once it has been read, as a plain function of its data: the mount above
  fetches it, this draws it, and the test can draw a report without a fetch or a
  state update behind it. `opts.onReread` is the only thing here that acts, and it
  only re-reads.
*/
function needsPanel(data, opts) {
  const o = opts || {}
  const needs = (data && data.needs) || []
  const waiting = (data && data.waiting) || []
  // A kind that is about an app gets a route to that app; the rest are already
  // visible in this window (the shelf below) or act on their own (a queued launch).
  const APP_KIND = { broken: 1, stopped: 1, update: 1 }
  const tone = (k) => (k === 'broken' || k === 'stopped') ? 'warn' : 'info'

  return jsxs('div', { children: [
    jsxs('div', { className: 'ah-row', children: [
      jsx('div', { className: 'ah-secthead', style: { flex: '1 1 auto' },
        children: 'What needs you' }),
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: !!o.busy,
        onClick: () => { if (o.onReread) o.onReread() },
        children: o.busy ? 'Reading…' : 'Re-read' }),
    ] }),

    jsx('div', { className: needs.length ? 'ah-needhead' : 'ah-needquiet',
      children: data.headline || '…' }),

    needs.map((n, i) => jsxs('div', {
      key: (n.kind || '') + (n.app || n.folder || i),
      className: 'ah-need ' + tone(n.kind), children: [
        jsxs('div', { className: 'ah-row', children: [
          jsx('span', { className: 'ah-needtitle', style: { flex: '1 1 auto' },
            children: n.title || n.kind }),
          jsxs('span', { className: 'ah-needwho', children: [
            n.app || (n.apps || []).join(', ') || n.folder || '',
            n.behind ? `  ${n.behind} behind` : '',
          ] }),
        ] }),
        n.detail ? jsx('div', { className: 'ah-needwhy', children: n.detail }) : null,
        jsxs('div', { className: 'ah-needrow', children: [
          // The step in words. It is what the button on the app's card will say,
          // so the two cannot drift into two different names for one action.
          jsx('span', { className: 'ah-sub', style: { flex: '1 1 auto' },
            children: (n.next_step && n.next_step.label)
              ? '→ ' + n.next_step.label
              : (n.fix && n.fix.label) ? '→ ' + n.fix.label : '' }),
          (APP_KIND[n.kind] && (n.app || (n.apps || []).length))
            ? jsx(Button, { size: 'micro', variant: 'ghost',
                onClick: () => tab.set('apps'),
                children: 'Show it' })
            : null,
        ] }),
      ],
    }, (n.kind || '') + (n.app || n.folder || i))),

    waiting.length ? jsxs('div', { className: 'ah-need info', children: [
      jsx('div', { className: 'ah-needtitle', children:
        waiting.length === 1 ? 'A launch is queued for the card'
                             : waiting.length + ' launches are queued for the card' }),
      jsx('div', { className: 'ah-needwhy', children:
        'It starts on its own when the card has room, and can be taken back on the '
        + 'Terminal. This queue lives in the dashboard: closing it forgets the wait '
        + 'rather than starting something nobody is watching.' }),
    ] }) : null,

    // What was read, in the report's own words. An empty list is otherwise
    // indistinguishable from a machine nothing was looked at on.
    (data.notes || []).length
      ? jsx('div', { className: 'ah-sub', style: { marginTop: 6 },
          children: (data.notes || []).join(' · ') })
      : null,
  ] })
}

function WeightShelf({ ctx }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [repo, setRepo] = useState('')
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [term, setTerm] = useState('')

  // Weighing every library is a walk of everything on the disk, so this is loaded
  // when the pane opens and when asked — never on a poll, which is what the
  // monitor's own tick must not become.
  const load = useCallback(async () => {
    try {
      const d = await ctx.rest('/models', { timeoutMs: 180000 })
      if (d && !d.error) { setData(d); setErr('') }
      else setErr((d && d.error) || 'unavailable')
    } catch (e) { setErr(String(e)) }
  }, [ctx])

  useEffect(() => { load() }, [load])

  const pull = async () => {
    const want = repo.trim()
    if (!want) return
    setBusy(true); setTerm(''); setMsg('')
    try {
      const r = await ctx.rest('/models/download', {
        method: 'POST', timeoutMs: 60000, body: { repo: want, app: target || null } })
      if (!r || !r.ok) { setMsg('Failed: ' + ((r && r.error) || 'unknown')); return }
      setMsg('Pulling ' + r.repo + ' — ' + fmtBytes(r.bytes || 0) + ' into '
        + (r.dest || 'the shared cache') + (r.already_cached
          ? ' (already cached here, so a copy costs nothing)' : ''))
      const out = await awaitJob(ctx, r.job, { onTick: (j) => setTerm(j.tail || '') })
      setTerm((out.job && out.job.tail) || '')
      const res = out.result || {}
      setMsg(out.ok
        ? ('Done: ' + fmtBytes(res.bytes_written || 0) + ' written'
           + (res.resumed_from ? ' (resumed from ' + fmtBytes(res.resumed_from) + ')' : '')
           + (res.cache_only ? ' — cached, and no app reads it yet' : ''))
        : ('Failed: ' + (out.error || 'unknown')))
      await load()
    } catch (e) { setMsg('Failed: ' + e) } finally { setBusy(false) }
  }

  const d = data || {}
  const apps = d.apps || []
  const need = d.need_weights || []
  const libs = (d.libraries || []).filter(l => l.total || l.bytes)
  const cached = (d.cached && d.cached.repos) || []

  return jsxs('div', { children: [
    jsx('div', { className: 'ah-secthead', children: 'Weights on this disk' }),
    jsx('div', { className: 'ah-sub', style: { marginBottom: 6 }, children:
      'What each app can generate with right now, where every weight library is and '
      + 'what it costs, and what nothing reads any more. Cached repositories are '
      + 'listed apart from that: a cached one is read whenever an app asks for it '
      + 'by name, so pulling it again never touches the network.' }),

    err && !data
      ? jsx('div', { className: 'ah-sub', children: 'Not available yet: ' + err })
      : jsxs('div', { children: [
          jsx('div', { className: 'ah-sub', children: d.summary || '…' }),
          need.length ? jsx('div', { className: 'ah-warn', style: { marginTop: 4 },
            children: need.length + ' cannot generate: ' + need.join(', ')
              + ' — pull a weight below, or point them at a library that has one.' })
            : null,

          libs.length ? jsxs('div', { style: { marginTop: 8 }, children: [
            jsx('div', { className: 'ah-mm-group', children:
              libs.length > 1 ? 'Libraries' : 'Library' }),
            libs.map(l => jsxs('div', { key: l.folder, className: 'ah-kv', children: [
              jsx('span', { style: { overflowWrap: 'anywhere' }, children: [
                jsx('span', { children: l.folder }),
                jsx('span', { className: l.apps.length ? 'ah-sub' : 'ah-warn', children:
                  l.apps.length ? '  read by ' + l.apps.join(', ')
                    : (l.empty ? '  nothing reads this'
                               : '  nothing reads this — ' + fmtBytes(l.bytes || 0)
                                 + ' doing nothing') }),
              ] }),
              jsx('span', { className: l.empty ? 'ah-sub' : '', children:
                (l.empty ? 'no weights to generate with' : l.total + ' weights')
                + (l.bytes ? ' · ' + fmtBytes(l.bytes) : '') }),
            ] }, l.folder)),
          ] }) : null,

          cached.length ? jsxs('div', { style: { marginTop: 8 }, children: [
            jsx('div', { className: 'ah-mm-group', children: 'Cached repositories' }),
            cached.slice(0, 8).map(r => jsx('div', { key: r.repo, className: 'ah-kv',
              children: [
                jsx('span', { children: r.repo }),
                jsx('span', { className: 'ah-sub', children: fmtBytes(r.bytes || 0) }),
              ] }, r.repo)),
            cached.length > 8 ? jsx('div', { className: 'ah-sub', children:
              'and ' + (cached.length - 8) + ' more' }) : null,
          ] }) : null,
        ] }),

    jsxs('div', { className: 'ah-row', style: { marginTop: 10 }, children: [
      jsx('input', {
        className: 'ah-searchin', type: 'text', value: repo,
        placeholder: 'owner/name on HuggingFace', 'aria-label': 'Repository to pull',
        disabled: busy, onChange: (e) => setRepo(e.target.value),
      }),
      jsx('select', {
        className: 'ah-searchin', value: target, 'aria-label': 'App the pulled file should serve',
        disabled: busy, onChange: (e) => setTarget(e.target.value),
        children: [jsx('option', { value: '', children: 'cache only (no app yet)' })].concat(
          apps.map(a => jsx('option', { key: a.app, value: a.app,
            children: a.app + (a.ready ? '' : ' (cannot generate)') }))),
      }),
      jsx(Button, { size: 'micro', disabled: busy || !repo.trim(), onClick: pull,
        children: busy ? 'Pulling…' : 'Pull' }),
      jsx(Button, { size: 'micro', variant: 'ghost', disabled: busy, onClick: load,
        children: 'Recheck' }),
    ] }),

    term ? jsx(AutoTerm, { text: term }) : null,
    msg ? jsx('div', { className: 'ah-sub', children: msg }) : null,
  ] })
}

/* ---------------------------------------------------------------- viewer */

// The app's own web interface, exactly as it serves it, with the output running
// behind it. An app is a server: launching one starts a process that prints for
// a while and then binds a port. Without somewhere to put that port, a launch
// that worked perfectly still looked like a button that did nothing.
function Viewer({ ctx }) {
  const app = useValue(viewer)
  const [sub, setSub] = useState('app')
  const [url, setUrl] = useState('')
  const [log, setLog] = useState('')
  const [alive, setAlive] = useState(null)
  const [nonce, setNonce] = useState(0)
  const [busy, setBusy] = useState('')
  const [ticks, setTicks] = useState(0)
  const termRef = useRef(null)
  const stick = useRef(true)
  // Once the user picks a tab themselves, stop moving it for them.
  const autoTab = useRef(true)
  const id = app ? app.id : null

  useEffect(() => {
    if (!app) return
    autoTab.current = true
    setTicks(0)
    setUrl(app.url || '')
    // An app with no address yet is showing its output, not a blank frame.
    setSub(app.url ? 'app' : 'terminal')
  }, [id])

  const poll = useCallback(async () => {
    if (!id) return
    try {
      const s = await ctx.rest(`/apps/${encodeURIComponent(id)}/status`, { timeoutMs: 15000 })
      setAlive(!!s.running)
      // The port is bound a while after Launch, and nothing observable happens
      // in between. So rather than guess at the wait, watch: the moment an
      // address appears, the app's own interface replaces the output view.
      if (s.url) {
        setUrl(s.url)
        if (autoTab.current) setSub('app')
      }
      setLog(s.log_tail || '')
    } catch { setAlive(false) }
    setTicks(n => n + 1)
  }, [ctx, id])

  useEffect(() => {
    if (!id) return
    poll()
    const t = setInterval(poll, 2500)
    return () => clearInterval(t)
  }, [poll, id])

  // Follow the output unless the user has scrolled up to read something.
  useEffect(() => {
    const el = termRef.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [log])

  // Tell Athena this app is still being looked at, so the idle reaper leaves it
  // alone. Athena cannot see clicks inside the app itself — the frame below
  // loads the app's own address, not something Athena proxies — so this window
  // being open, visible and pointed at a running app is the honest signal.
  // Closing it hands the app back, and it stops once nothing is watching.
  useEffect(() => {
    if (!id) return
    const ping = () => {
      if (!url || alive === false) return
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      ctx.rest(`/apps/${encodeURIComponent(id)}/activity`,
        { method: 'POST', timeoutMs: 8000 }).catch(() => {})
    }
    ping()
    const t = setInterval(ping, 30000)
    return () => clearInterval(t)
  }, [ctx, id, url, alive])

  if (!app) {
    return jsxs('div', { className: 'ah-vwait', children: [
      jsx('div', { className: 'ah-title', children: 'Nothing open' }),
      jsx('div', { className: 'ah-sub', children: 'Launch an app from the Apps tab. Its own interface opens here, and whatever it prints is on the Terminal tab beside it.' }),
    ] })
  }

  const stop = async () => {
    setBusy('stop')
    try {
      const r = await ctx.rest(`/apps/${encodeURIComponent(id)}/stop`,
        { method: 'POST', timeoutMs: 90000 })
      // A stop that was refused must not be dressed as a stop: clearing the frame below is the
      // right move for a stop that happened, and a lie about one that did not.
      const refused = actionFailure('Stopping it', r)
      if (refused) { notify(ctx, 'error', refused); return }
      // Leaving a frame pointed at a port nothing is listening on shows the
      // browser's own "can't reach this page", which reads as Athena being
      // broken. Stopping should close the app's interface, not strand it.
      setUrl('')
      setLog('')
      setAlive(false)
      // ...and hand the user back to their apps, which is where they were.
      viewer.set(null)
      tab.set('apps')
      closeViewerWindow()
      revealAthena()
      const n = (r && r.count) || 0
      notify(ctx, 'info', n
        ? `Stopped ${app.name || app.id}.`
        : `${app.name || app.id} was not running.`)
    } catch (e) { notify(ctx, 'error', `Stop: ${e}`) } finally { setBusy('') }
  }
  const restart = async () => {
    setBusy('restart')
    setLog('Restarting…\n')
    try {
      const r = await ctx.rest(`/apps/${encodeURIComponent(id)}/launch`, { method: 'POST', timeoutMs: 300000 })
      // A launch with no usable Node answers 200 with `ok: false`; without this the log stopped at
      // "Restarting…" and the app stayed stopped, which is the silence the pass was about.
      const refused = actionFailure('Launching it', r)
      if (refused) { notify(ctx, 'error', refused); setLog((p) => p + refused + '\n'); return }
      if (r && r.url) { setUrl(r.url); setSub('app'); setNonce(n => n + 1) }
      else if (r && r.url_note) setLog((p) => p + r.url_note + '\n')
      await poll()
    } catch (e) { notify(ctx, 'error', `Launch: ${e}`) } finally { setBusy('') }
  }
  const openBrowser = () => {
    if (!url) return
    try {
      if (host.openExternal) host.openExternal(url)
      else window.open(url, '_blank')
    } catch { /* leave it in the frame */ }
  }

  const tabBtn = (key, label, disabled) => jsx('button', {
    className: 'ah-tabb' + (sub === key ? ' active' : ''),
    disabled: disabled,
    onClick: () => { autoTab.current = false; setSub(key) },
    children: label,
  }, key)

  return jsxs('div', { className: 'ah-vroot', children: [
    jsxs('div', { className: 'ah-vhead', children: [
      jsx('span', {
        className: 'ah-dot' + (alive ? ' on' : ''),
        title: alive ? 'running' : 'stopped',
      }),
      jsxs('div', { style: { minWidth: 0, flex: '0 1 auto' }, children: [
        jsx('div', { className: 'ah-vname', children: app.name }),
        jsx('div', { className: 'ah-vurl', children: url || 'not serving a web interface' }),
      ] }),
      jsx('div', { className: 'ah-grow' }),
      jsx('button', {
        className: 'ah-btn', disabled: !!busy || !alive,
        onClick: stop, children: busy === 'stop' ? 'Stopping…' : 'Stop',
      }),
      jsx('button', {
        className: 'ah-btn', disabled: !!busy,
        onClick: restart, children: busy === 'restart' ? 'Starting…' : 'Restart',
      }),
      jsx('button', { className: 'ah-btn', disabled: !url, onClick: openBrowser, children: 'Browser' }),
    ] }),
    jsxs('div', { className: 'ah-tabs', style: { paddingTop: 4 }, children: [
      tabBtn('app', 'App', !url),
      tabBtn('terminal', 'Terminal', false),
    ] }),
    sub === 'app' && url
      ? jsx('iframe', {
          className: 'ah-vframe', src: url, key: url + ':' + nonce,
          title: app.name, allow: 'clipboard-write; fullscreen',
          sandbox: 'allow-same-origin allow-scripts allow-forms allow-popups allow-downloads allow-modals',
        })
      : null,
    sub === 'app' && !url
      ? jsxs('div', { className: 'ah-vwait', children: [
          jsx('div', { children: alive
            ? 'Running, but this app has not opened a web interface.'
            : (ticks < 24 ? 'Starting…' : 'Still no interface from this app.') }),
          jsx('div', { className: 'ah-sub', children: ticks < 24
            ? 'Its address appears here the moment it is listening. Everything it has printed is on the Terminal tab.'
            : 'It may only have a command line. Its output is on the Terminal tab.' }),
          ticks < 24 && !alive
            ? jsx('div', { className: 'ah-bar', style: { width: 160 }, children:
                jsx('div', { className: 'ah-barfill indet' }) })
            : null,
          jsx('button', {
            className: 'ah-btn',
            onClick: () => { autoTab.current = false; setSub('terminal') },
            children: 'Show Terminal',
          }),
        ] })
      : null,
    sub === 'terminal'
      ? jsx('pre', {
          className: 'ah-vterm', ref: termRef,
          onScroll: (e) => {
            const el = e.currentTarget
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
          },
          children: log || (alive ? 'Running. No output yet.' : 'Not running.'),
        })
      : null,
  ] })
}

/* A collapsible settings section. The pane had grown to five headings with
   everything open at once, which turned finding one switch into reading all of
   them. A section folds, remembers whether it was folded, and shows a one-line
   summary while it is closed so the page still reads as an answer rather than a
   list of titles. */
function Section({ ctx, id, title, summary, defaultOpen, children }) {
  const key = 'sec:' + id
  const [open, setOpen] = useState(() => {
    try {
      const v = ctx.storage.get(key)
      return v == null ? !!defaultOpen : v === true
    } catch { return !!defaultOpen }
  })
  const toggle = () => {
    const v = !open
    setOpen(v)
    try { ctx.storage.set(key, v) } catch { /* noop */ }
  }
  return jsxs('div', { className: 'ah-sec' + (open ? ' ah-sec-open' : ''), children: [
    jsx('button', {
      type: 'button', className: 'ah-secb',
      'aria-expanded': open ? 'true' : 'false',
      onClick: toggle,
      children: jsxs('span', { className: 'ah-row', style: { flex: 1, minWidth: 0 }, children: [
        jsx('span', { className: 'ah-secchev', children: open ? '\u25be' : '\u25b8' }),
        jsx('span', { className: 'ah-sectitle', children: title }),
        (!open && summary) ? jsx('span', { className: 'ah-secsum', children: summary }) : null,
      ] }),
    }),
    open ? jsx('div', { className: 'ah-secbody', children }) : null,
  ] })
}

/* The plugin's own settings, which live on the backend beside the engine's port
   and home. Read once when the pane mounts and written through on every flip:
   the switch moves on the optimistic value first, because a switch that waits
   for a round trip before it moves reads as a broken switch. */
function useSettings(ctx) {
  const [cfg, setCfg] = useState(null)
  useEffect(() => {
    let live = true
    ctx.rest('/settings', { timeoutMs: 15000 })
      .then((r) => { if (live) setCfg(r || {}) })
      .catch(() => { if (live) setCfg({}) })
    return () => { live = false }
  }, [ctx])
  const save = useCallback(async (patch) => {
    setCfg((c) => ({ ...(c || {}), ...patch }))
    try {
      const r = await ctx.rest('/settings', { method: 'POST', body: patch, timeoutMs: 15000 })
      // The optimistic value is set before the call, so a refusal has to take it back and say why
      // — otherwise the panel shows a setting the backend never accepted.
      const refused = actionFailure('Saving that setting', r)
      if (refused) notify(ctx, 'error', refused)
      else if (r) setCfg(r)
    } catch { /* keep the optimistic value; the next mount re-reads */ }
  }, [ctx])
  return [cfg, save]
}

function SettingsTab({ ctx, st, refresh }) {
  const [creds, setCreds] = useState(null)
  const [scan, setScan] = useState(null)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState(null)   // null = everything so far
  const [term, setTerm] = useState('')
  const [impProg, setImpProg] = useState(null)

  // The scan answers straight away and measures app sizes in the background, so
  // it reports "sizing" and is polled until that clears. Sizes arrive as they
  // are worked out rather than holding the whole panel hostage to a walk over
  // hundreds of gigabytes.
  const loadScan = useCallback(async () => {
    try {
      const s = await ctx.rest('/import/scan', { timeoutMs: 30000 })
      setScan(s)
      return s
    } catch { return null }
  }, [ctx])

  useEffect(() => {
    ctx.rest('/credentials', { timeoutMs: 30000 }).then(setCreds).catch(() => {})
    let stopped = false
    let timer = null
    const poll = async () => {
      const s = await loadScan()
      if (stopped) return
      if (s && s.sizing) timer = setTimeout(poll, 2500)
    }
    poll()
    return () => { stopped = true; if (timer) clearTimeout(timer) }
  }, [ctx, loadScan])

  const seed = async () => {
    setBusy(true)
    try {
      const r = await ctx.rest('/credentials/seed', { method: 'POST', timeoutMs: 60000 })
      const refused = actionFailure('Sharing credentials', r)
      setMsg(refused || `Shared ${(r.written || []).length} credential file(s).`)
    } catch (e) { setMsg('Failed: ' + e) } finally { setBusy(false) }
  }

  const allIds = (scan && scan.apps) ? scan.apps.map(a => a.id) : []
  const chosen = selected === null ? allIds : allIds.filter(id => selected.has(id))

  const toggle = (id) => {
    setSelected((prev) => {
      const s = new Set(prev === null ? allIds : prev)
      if (s.has(id)) s.delete(id); else s.add(id)
      return s
    })
  }
  const selectAll = () => setSelected(null)
  const selectNone = () => setSelected(new Set())

  // Importing can be a multi-hundred-gigabyte copy, so it runs as a job and is
  // watched rather than waited on.
  const doImport = async (mode) => {
    const ids = chosen
    if (!ids.length) { setMsg('Nothing selected.'); return }
    setBusy(true)
    setTerm('')
    setImpProg(null)
    setMsg(mode === 'adopt'
      ? `Linking ${ids.length} app(s) — no files are copied, so this is quick…`
      : `Copying ${ids.length} app(s) — this can take a long time where apps have models…`)
    try {
      const r = await ctx.rest('/import/run', {
        method: 'POST', timeoutMs: 60000,
        body: { source: scan.source_home, apps: ids, mode },
      })
      if (!r || !r.ok) { setMsg('Import failed: ' + ((r && r.error) || 'unknown')); return }
      const out = await awaitJob(ctx, r.job, {
        onTick: (j) => { setTerm(j.tail || ''); setImpProg(j && j.progress) },
      })
      setTerm((out.job && out.job.tail) || '')
      setImpProg((out.job && out.job.progress) || null)
      if (!out.ok) { setMsg('Import failed: ' + out.error); return }
      const results = (out.result && out.result.results) || []
      const ok = results.filter(x => x.ok).length
      const failed = results.filter(x => !x.ok)
      setMsg(`${ok}/${results.length} apps imported`
        + (failed.length ? ` — ${failed.length} failed: ${failed[0].error}` : '.'))
      await refresh()
      setSelected(null)
      await loadScan()
    } catch (e) { setMsg('Import failed: ' + e) } finally { setBusy(false) }
  }

  const avail = (k) => !!(creds && creds[k] && creds[k].available)
  // Read through the atoms, not the storage: flipping a switch in this very
  // pane has to move the switch, not just the setting behind it.
  const barOn = useValue(sysBar)
  const [cfg, saveCfg] = useSettings(ctx)
  // Absent means "never written", and absent has to mean on: the default is on.
  const autoOn = !cfg || cfg.autostart !== false
  // The idle window is a number the user types, so it needs a draft: typing
  // "45" passes through "4" on the way, and saving each keystroke would clamp
  // and rewrite the field under them mid-word.
  const idleSecs = (cfg && typeof cfg.idle_stop_seconds === 'number')
    ? cfg.idle_stop_seconds : 120
  const [idleDraft, setIdleDraft] = useState(String(idleSecs))
  useEffect(() => { setIdleDraft(String(idleSecs)) }, [idleSecs])
  // The store source is typed, not toggled, so it needs the same draft care as
  // the idle window: saving per keystroke would rewrite the field mid-URL.
  const storeSource = (cfg && cfg.store_index) || 'Athena Store'
  const [storeDraft, setStoreDraft] = useState((cfg && cfg.store_index) || '')
  useEffect(() => { setStoreDraft((cfg && cfg.store_index) || '') }, [cfg && cfg.store_index])
  const ghReady = !!(creds && creds.github && creds.github.available)
  const saveStore = () => saveCfg({ store_index: String(storeDraft || '').trim() })
  const saveIdle = () => {
    const n = Math.max(0, Math.round(Number(idleDraft)))
    if (!isFinite(n)) { setIdleDraft(String(idleSecs)); return }
    setIdleDraft(String(n))
    saveCfg({ idle_stop_seconds: n })
  }
  // The two headroom lines: gigabytes, typed, so they need the same draft care
  // as the idle window. 0 means "never speak up about this one".
  const diskWarnGb = (cfg && typeof cfg.disk_warn_free_gb === 'number') ? cfg.disk_warn_free_gb : 10
  const [diskDraft, setDiskDraft] = useState(String(diskWarnGb))
  useEffect(() => { setDiskDraft(String(diskWarnGb)) }, [diskWarnGb])
  const vramWarnGb = (cfg && typeof cfg.vram_warn_free_gb === 'number') ? cfg.vram_warn_free_gb : 6
  const [vramDraft, setVramDraft] = useState(String(vramWarnGb))
  useEffect(() => { setVramDraft(String(vramWarnGb)) }, [vramWarnGb])
  const saveHeadroom = (draft, setDraft, fallback, key) => {
    const n = Math.max(0, Number(draft))
    if (!isFinite(n)) { setDraft(String(fallback)); return }
    setDraft(String(n))
    saveCfg({ [key]: n })
  }
  const saveDisk = () => saveHeadroom(diskDraft, setDiskDraft, diskWarnGb, 'disk_warn_free_gb')
  const saveVram = () => saveHeadroom(vramDraft, setVramDraft, vramWarnGb, 'vram_warn_free_gb')
  const appsRow = (cfg && cfg.apps_layout) === 'rows'
  return jsxs('div', { className: 'ah-set', children: [
    jsx(Section, { ctx, id: 'engine', title: 'Engine', defaultOpen: true,
      summary: st ? (st.ready ? 'Ready' : (st.running ? 'Starting' : 'Stopped')) : null,
      children: st ? jsxs('div', { children: [
        jsx(ListRow, {
          title: 'Status',
          description: 'The local engine that installs and runs your apps.',
          action: jsxs('span', { className: 'ah-row', children: [
            jsx(StatusDot, { tone: st.ready ? 'good' : (st.running ? 'warn' : 'muted') }),
            jsx('span', { children: st.ready ? 'Ready' : (st.running ? 'Starting' : 'Stopped') }),
          ] }),
        }),
        jsx(ListRow, {
          title: 'Port',
          description: 'Where Hermes reaches the engine on this machine.',
          action: jsx('code', { className: 'ah-mono', children: String(st.port) }),
        }),
        jsx(ListRow, {
          title: 'Home',
          description: 'Every installed app, its Python environment and its model cache live here.',
          hint: st.home,
        }),
        jsx(ListRow, {
          title: 'Node',
          description: 'The bundled runtime the engine runs on.',
          action: jsx('code', { className: 'ah-mono', children: st.node || 'not found' }),
        }),
        jsx(ListRow, {
          title: 'Stop apps when idle',
          description: 'An app left running after you are done with it keeps its model on the ' +
            'graphics card, so Athena stops it once nothing has used it for this long. ' +
            'Using it again — from here, from the agent, or by leaving its window open — ' +
            'starts the clock over. Set 0 to never stop apps by themselves.',
          action: jsxs('span', { className: 'ah-row', children: [
            jsx('input', {
              className: 'ah-num', type: 'number', min: 0, step: 30,
              value: idleDraft, 'aria-label': 'Idle stop, seconds',
              onChange: (e) => setIdleDraft(e.target.value),
              onBlur: saveIdle,
              onKeyDown: (e) => { if (e.key === 'Enter') saveIdle() },
            }),
            jsx('span', { className: 'ah-sub', children: idleSecs > 0 ? 'seconds' : 'never' }),
          ] }),
          hint: idleSecs > 0
            ? `Currently: stopped ${idleSecs}s after the last use (30s minimum).`
            : 'Apps are never stopped automatically.',
        }),
        jsx(ListRow, {
          title: 'Warn before an install fills the drive',
          description: 'An app\'s install clones a repository and builds a Python or Node ' +
            'environment into your Athena folder, and usually pulls a model too. Athena says ' +
            'how much room is left before it starts; below 2 GB it refuses outright, because ' +
            'nothing can be written. Set 0 to stop the warning.',
          action: jsxs('span', { className: 'ah-row', children: [
            jsx('input', {
              className: 'ah-num', type: 'number', min: 0, step: 1,
              value: diskDraft, 'aria-label': 'Warn below this much free disk, GB',
              onChange: (e) => setDiskDraft(e.target.value),
              onBlur: saveDisk,
              onKeyDown: (e) => { if (e.key === 'Enter') saveDisk() },
            }),
            jsx('span', { className: 'ah-sub', children: diskWarnGb > 0 ? 'GB free' : 'off' }),
          ] }),
          hint: diskWarnGb > 0
            ? `Currently: a warning when the drive has under ${diskWarnGb} GB free.`
            : 'Athena installs without mentioning the disk.',
        }),
        jsx(ListRow, {
          title: 'Warn when the card is nearly full',
          description: 'A loaded model can hold tens of gigabytes of VRAM, and an app that ' +
            'starts second is the one that dies. Before a launch, Athena says how much of the ' +
            'card is free and who is holding the rest. Set 0 to stop the warning.',
          action: jsxs('span', { className: 'ah-row', children: [
            jsx('input', {
              className: 'ah-num', type: 'number', min: 0, step: 1,
              value: vramDraft, 'aria-label': 'Warn below this much free VRAM, GB',
              onChange: (e) => setVramDraft(e.target.value),
              onBlur: saveVram,
              onKeyDown: (e) => { if (e.key === 'Enter') saveVram() },
            }),
            jsx('span', { className: 'ah-sub', children: vramWarnGb > 0 ? 'GB free' : 'off' }),
          ] }),
          hint: vramWarnGb > 0
            ? `Currently: a warning when the card has under ${vramWarnGb} GB free.`
            : 'Athena launches without mentioning the card.',
        }),
        jsx('div', { style: { marginTop: 8 }, children: jsx(ToggleRow, {
          checked: autoOn,
          label: 'Start the engine with Hermes',
          description: 'Athena brings the engine up by itself the first time Hermes asks it ' +
            'anything, so apps are reachable without pressing Start. Turn this off to leave ' +
            'the engine stopped until you start it yourself.',
          onChange: (v) => saveCfg({ autostart: v }),
        }) }),
        (st.autostart && st.autostart.tried && st.autostart.ok === null && !st.autostart.disabled)
          ? jsx('div', { className: 'ah-sub', style: { marginTop: 6 },
              children: 'Bringing the engine up\u2026' })
          : null,
        (st.autostart && st.autostart.error)
          ? jsx('div', { className: 'ah-sub', style: { marginTop: 6 },
              children: 'The engine did not start: ' + st.autostart.error })
          : null,
      ] }) : jsx('div', { className: 'ah-sub', children: 'Reading engine state\u2026' }) }),

    jsx(Section, { ctx, id: 'store', title: 'Store',
      summary: (cfg && cfg.store_index) || 'Athena Store',
      children: jsxs('div', { children: [
        jsx(ListRow, {
          title: 'Where apps come from',
          description: 'The store reads an index of apps; each app is a branch of that ' +
            'repository, so installing one clones it and Update still works afterwards. ' +
            'Leave this empty to read the Athena Store. The public catalogue is browsed ' +
            'behind it either way, so nothing is lost by pointing this elsewhere.',
          action: jsxs('span', { className: 'ah-row', children: [
            jsx('div', { style: { flex: '1 1 220px', minWidth: 0 }, children: jsx(Field, {
              value: storeDraft, placeholder: 'owner/repo (empty = Athena Store)',
              onChange: setStoreDraft,
              onSubmit: saveStore,
            }) }),
            jsx(Button, { size: 'micro', onClick: saveStore, children: 'Save' }),
          ] }),
          hint: storeSource,
        }),
        jsx(ListRow, {
          title: 'Private repositories',
          description: 'Read with the GitHub account Hermes is already signed in to, so a ' +
            'private store needs no extra sign-in and no token to paste.',
          action: jsx('span', { className: 'ah-badge ' + (ghReady ? 'run' : ''), children:
            ghReady ? 'signed in' : 'no GitHub login' }),
        }),
      ] }) }),

    jsx(Section, { ctx, id: 'monitor', title: 'System monitor',
      summary: barOn ? 'In the status bar' : 'Hidden from the status bar',
      children: jsxs('div', { children: [
        jsx(ToggleRow, {
          checked: barOn,
          label: 'Mini monitor in the status bar',
          description: 'A compact CPU \u00b7 RAM \u00b7 GPU readout at the bottom right of Hermes, ' +
            'beside the music widget.',
          onChange: (v) => setSysBar(ctx, v),
        }),
        jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children:
          'Click the readout for drives, network and the heaviest processes, and for the full ' +
          'monitor, which opens in its own window.' }),
      ] }) }),

    // How the Apps tab draws an app. One setting, two words, and nothing else in
    // the panel reads it, so this stays a display choice rather than a mode.
    jsx(Section, { ctx, id: 'apps', title: 'Apps',
      summary: appsRow ? 'One row per app' : 'One card per app',
      children: jsxs('div', { children: [
        jsx(ToggleRow, {
          checked: appsRow,
          label: 'List apps as rows',
          description: 'Rows fit far more apps on one screen and clip the description to a single ' +
            'line. Cards show the description and the tags in full, which is what tells two ' +
            'similarly named apps apart. Nothing else changes: the same state chip, the same ' +
            'button and the same menu are on both.',
          onChange: (v) => saveCfg({ apps_layout: v ? 'rows' : 'cards' }),
        }),
      ] }) }),

    jsx(Section, { ctx, id: 'credentials', title: 'Credentials from Hermes',
      summary: creds ? (['github', 'huggingface'].filter(avail).length + ' of 2 inherited')
        : 'Reading\u2026',
      children: jsxs('div', { children: [
        jsx('div', { className: 'ah-sub', children:
          'Athena reuses what Hermes already holds. You should never have to sign in twice.' }),
        creds ? jsxs('div', { children: [
          jsx(ListRow, {
            title: 'GitHub',
            description: 'Adding your own apps from a repository.',
            action: jsx(Badge, { variant: avail('github') ? 'success' : 'muted',
              children: avail('github') ? 'Inherited' : 'Not found' }),
          }),
          jsx(ListRow, {
            title: 'HuggingFace',
            description: 'Pulling models without a second sign-in.',
            action: jsx(Badge, { variant: avail('huggingface') ? 'success' : 'muted',
              children: avail('huggingface') ? 'Inherited' : 'Not found' }),
          }),
          jsx(ListRow, {
            title: 'Provider keys',
            description: 'Handed to any app that asks for a model key.',
            action: jsx('span', { className: 'ah-sub',
              children: Object.keys(creds.providers || {}).join(', ') || 'none' }),
          }),
        ] }) : jsx('div', { className: 'ah-sub', children: 'Loading\u2026' }),
        jsx('div', { className: 'ah-row', style: { marginTop: 8 }, children:
          jsx(Button, { size: 'micro', disabled: busy, onClick: seed, children: 'Share now' }) }),
      ] }) }),

    jsx(Section, { ctx, id: 'import', title: 'Import from a Pinokio install',
      summary: scan === null ? 'Looking\u2026'
        : ((scan.apps && scan.apps.length)
            ? scan.apps.length + ' app' + (scan.apps.length === 1 ? '' : 's') + ' found'
            : 'None found'),
      children: jsxs('div', { children: [
        // scan === null means the question is still out. Saying "none detected"
        // before the answer arrives is what made this look like it had failed,
        // when in fact the first version was quietly walking 264 GB.
        scan === null
          ? jsx('div', { className: 'ah-sub', children: 'Looking for a Pinokio install\u2026' })
          : (scan.apps && scan.apps.length)
          ? jsxs('div', { children: [
              jsxs('div', { className: 'ah-sub', children: [
                jsx('div', { children: 'Found a Pinokio install on this machine:' }),
                jsx('div', { className: 'ah-repo', style: { marginTop: 2 },
                             children: scan.source_home }),
                jsx('div', { style: { marginTop: 6 }, children:
                  `${scan.apps.length} app${scan.apps.length === 1 ? '' : 's'} \u00b7 ` +
                  `${scan.total_size_complete ? fmtBytes(scan.total_size) : fmtBytes(scan.total_size) + '+'} on disk` +
                  (scan.sizing ? ' \u2014 still measuring\u2026' : '.') +
                  ' Importing brings across each app folder, which is where models ' +
                  'live, since apps keep their downloads inside themselves.' }),
              ] }),

              jsxs('div', { className: 'ah-row', style: { marginTop: 10 }, children: [
                jsx('span', { className: 'ah-sub', children: `${chosen.length} of ${allIds.length} selected` }),
                jsx(Button, { size: 'micro', variant: 'ghost', disabled: busy,
                  onClick: selectAll, children: 'All' }),
                jsx(Button, { size: 'micro', variant: 'ghost', disabled: busy,
                  onClick: selectNone, children: 'None' }),
              ] }),

              jsx('div', { style: { marginTop: 6, maxHeight: 190, overflow: 'auto' }, children:
                scan.apps.map(a => jsxs('label', { className: 'ah-kv ah-pick', children: [
                  jsx('input', { type: 'checkbox', checked: chosen.includes(a.id),
                    disabled: busy || a.imported || !a.has_manifest,
                    onChange: () => toggle(a.id) }),
                  jsx('span', { children: a.id + (a.has_manifest ? ''
                    : '  (no launcher \u2014 cannot run)') }),
                  jsx('span', { children: (a.imported ? 'already imported \u00b7 ' : '')
                    + (a.size == null ? 'measuring\u2026' : fmtBytes(a.size)) }),
                ] }, a.id)) }),

              // Copying is the obvious default but it is the wrong one for an
              // install with models in it: the same bytes twice, for no gain on
              // the same disk.
              jsxs('div', { className: 'ah-row', style: { marginTop: 10 }, children: [
                jsx(Button, { size: 'micro', disabled: busy, onClick: () => doImport('adopt'),
                  children: busy ? 'Working\u2026' : 'Link in place (no extra disk)' }),
                jsx(Button, { size: 'micro', variant: 'ghost', disabled: busy,
                  onClick: () => doImport('copy'),
                  children: 'Copy into Athena' }),
              ] }),
              jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children:
                'Linking reuses the existing folders where they already are, so nothing ' +
                'is duplicated. Copying makes independent copies, at the cost of the space.' }),
            ] })
          : jsx('div', { className: 'ah-sub', children:
              'No Pinokio install detected on this machine.' }),

        impProg ? jsx(JobProgress, { progress: impProg }) : null,
        term ? jsx(AutoTerm, { text: term }) : null,
        msg ? jsx('div', { className: 'ah-sub', style: { marginTop: 10 }, children: msg }) : null,
      ] }) }),

    jsx(Section, { ctx, id: 'storage', title: 'Storage',
      summary: 'One copy of everything \u00b7 reclaim space',
      children: jsxs('div', { children: [
        jsx(SharedStore, { ctx }),
        jsx(DiskSaver, { ctx }),
        jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children:
          'Athena keeps everything inside the Hermes folder: each installed app, its Python ' +
          'environment, and its model cache live under the app home shown above.' }),
      ] }) }),
  ] })
}
/* ----------------------------------------------- what the card has cost */

/* A running total, not a gauge: it only moves while the monitor is alive, and a
   reading every 3 seconds is already more than this needs. Polled slowly on
   purpose — nothing here changes between two glances. */
const COST_POLL_MS = 30000

function useGpuCost(ctx, on) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    if (!on) return undefined
    let stopped = false
    const tick = async () => {
      try {
        const r = await ctx.rest('/cost', { timeoutMs: 8000 })
        if (!stopped) { setData(r || {}); setErr('') }
      } catch (e) { if (!stopped) setErr(String((e && e.message) || e)) }
    }
    tick()
    const timer = setInterval(tick, COST_POLL_MS)
    return () => { stopped = true; clearInterval(timer) }
  }, [ctx, on])
  return [data, err]
}

/* How long the card has been at it, in the units a person would say it in. */
function costSpan(seconds) {
  let s = Math.max(0, Math.floor(Number(seconds) || 0))
  const days = Math.floor(s / 86400); s -= days * 86400
  const hours = Math.floor(s / 3600); s -= hours * 3600
  const mins = Math.floor(s / 60)
  if (days) return days + 'd ' + hours + 'h'
  if (hours) return hours + 'h ' + mins + 'm'
  if (mins) return mins + 'm'
  return 'less than a minute'
}

/* Energy, at a resolution that does not round a short session to nothing. A
   kilowatt-hour is a big unit for one afternoon of work: showing "0.00" at two
   decimals would say the card used nothing when it used a little. */
function kwhLabel(v) {
  const n = Number(v) || 0
  if (n >= 100) return n.toFixed(0) + ' kWh'
  if (n >= 1) return n.toFixed(2) + ' kWh'
  if (n >= 0.001) return n.toFixed(3) + ' kWh'
  return n.toFixed(5) + ' kWh'
}

/* Money only ever arrives with a price attached, and it is rounded to cents only
   once cents are what it is; below that it keeps enough places to be a number
   rather than a zero. */
function moneyLabel(v) {
  const n = Number(v) || 0
  return '$' + (n >= 0.01 ? n.toFixed(2) : n.toFixed(4))
}

function priceLabel(v) {
  const n = Number(v) || 0
  // Two places for anything that is really a price. `0.21` printed as `0.210`
  // looks like a rounding artefact next to `$0.21` in the same sentence.
  return '$' + (n >= 0.01 ? n.toFixed(2) : n.toFixed(4))
}

/* The one sentence this whole feature exists to get right. Every branch is a
   state the card can actually be in, and none of them claims more than the
   record holds: no energy on a card that does not report power, no money without
   a price, and never a second of time that was not sampled. */
function costLine(d) {
  const x = d || {}
  if (!x.samples) return 'Nothing measured yet \u2014 this fills in while Hermes is open.'
  if (!x.seconds) return 'Reading the card; no time counted yet.'
  const span = costSpan(x.seconds)
  if (x.kwh == null) {
    return span + ' of the card\u2019s time so far \u2014 it does not report power draw, '
      + 'so the energy and the cost are left empty rather than reported as zero.'
  }
  let s = span + ' of card time, ' + kwhLabel(x.kwh) + ' drawn'
  if (x.busy_seconds && x.busy_seconds < x.seconds) {
    s += ' (' + costSpan(x.busy_seconds) + ' of it with the card working)'
  }
  if (x.cost == null) return s + ' \u2014 no price set, so this is not in money yet.'
  return s + ', about ' + moneyLabel(x.cost) + ' at ' + priceLabel(x.price) + '/kWh.'
}

/* The small print, which is the honest half of the feature: what was counted,
   what was not, and what the number really is. */
function costDetail(d) {
  const x = d || {}
  if (!x.samples) {
    return 'The monitor samples the card every few seconds; the record only grows '
      + 'while Hermes is open.'
  }
  const bits = ['counted only while Hermes was open and watching the card']
  bits.push('from the power nvidia-smi reported at each sample, so this is an estimate '
    + 'rather than a meter')
  if (x.peak_watts != null) bits.push('peak ' + Math.round(x.peak_watts) + ' W')
  if (x.gaps) bits.push(x.gaps + ' gap' + (x.gaps === 1 ? '' : 's') + ' not counted')
  if (x.since) bits.push('since ' + String(x.since).replace('T', ' '))
  return bits.join(' \u00b7 ')
}

function GpuCostCard({ ctx }) {
  const [d, err] = useGpuCost(ctx, true)
  const [price, setPrice] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const x = d || {}

  // The field shows what is stored, once, without fighting the person typing in
  // it: a poll landing mid-edit must not overwrite a half-typed price.
  useEffect(() => {
    if (d && price === '' && Number(d.price) > 0) setPrice(String(d.price))
  }, [d, price])

  const save = async () => {
    const v = Number(price)
    if (!isFinite(v) || v < 0) { setMsg('A price is a number per kWh, or 0 to leave it out.'); return }
    setBusy(true); setMsg('')
    try {
      const r = await ctx.rest('/cost/price', { method: 'POST', body: { price: v }, timeoutMs: 8000 })
      setMsg(r && r.ok ? 'Saved.' : 'Failed: ' + ((r && r.error) || 'unknown'))
    } catch (e) { setMsg('Failed: ' + e) } finally { setBusy(false) }
  }

  const restart = async () => {
    if (typeof window !== 'undefined' && !window.confirm(
      'Start the record over?\n\n'
      + 'The time and energy counted so far are cleared; the price is kept. '
      + 'This clears the total for every app, since it is one reading of the one card.')) return
    setBusy(true); setMsg('')
    try {
      const r = await ctx.rest('/cost/reset', { method: 'POST', timeoutMs: 8000 })
      setMsg(r && r.ok ? 'Started over.' : 'Failed: ' + ((r && r.error) || 'unknown'))
    } catch (e) { setMsg('Failed: ' + e) } finally { setBusy(false) }
  }

  return jsxs('div', { children: [
    jsx('div', { className: 'ah-secthead', children: 'What the card has cost' }),
    err && !d
      ? jsx('div', { className: 'ah-sub', children: 'No reading \u2014 the Athena backend is not answering.' })
      : jsxs('div', { children: [
          jsx('div', { className: 'ah-sub', children: costLine(x) }),
          jsx('div', { className: 'ah-sub', style: { marginTop: 4, opacity: 0.8 },
            children: costDetail(x) }),

          jsxs('div', { className: 'ah-row', style: { marginTop: 8 }, children: [
            jsx('span', { className: 'ah-sub', children: 'Price per kWh' }),
            jsx('input', { className: 'ah-num', type: 'number', min: '0', step: '0.01',
              value: price, placeholder: '0.21', disabled: busy,
              onChange: (e) => setPrice(e.target.value) }),
            jsx(Button, { size: 'micro', disabled: busy, onClick: save, children: 'Save' }),
            jsx(Button, { size: 'micro', variant: 'ghost', disabled: busy,
              onClick: restart, children: 'Start over' }),
          ] }),
          // Where the number comes from, said plainly: this is the one place a
          // reader is likely to over-trust the figure, and the price is theirs.
          jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children:
            'The energy is real but sampled \u2014 watts read every few seconds, not '
            + 'measured continuously \u2014 so treat the total as a good estimate. '
            + 'Leave the price at 0 to keep it in kWh only.' }),
          msg ? jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children: msg }) : null,
        ] }),
  ] })
}

/* --------------------------------------------------------- mini monitor */

/* Athena's own read of /sys/stats rather than the ported panel's. The chip has
   to stand on its own in the status bar — nothing else on screen has been
   asked whether the System tab was ever opened — and it polls on its own
   cadence because the chip and the tab's panel can be visible at once. */
const SYS_POLL_MS = 3000

function fmtBps(v) {
  const n = Number(v) || 0
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB/s'
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB/s'
  return n + ' B/s'
}

function fmtUptime(bootEpoch) {
  if (!bootEpoch) return '—'
  let s = Math.max(0, Math.floor(Date.now() / 1000) - Number(bootEpoch))
  const days = Math.floor(s / 86400); s -= days * 86400
  const hours = Math.floor(s / 3600); s -= hours * 3600
  const mins = Math.floor(s / 60)
  if (days) return days + 'd ' + hours + 'h'
  if (hours) return hours + 'h ' + mins + 'm'
  return mins + 'm'
}

function useSysStats(ctx, on) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(false)
  useEffect(() => {
    if (!on) return undefined
    let stopped = false
    const tick = async () => {
      try {
        const r = await ctx.rest('/sys/stats', { timeoutMs: 8000 })
        if (!stopped) { setData(r || {}); setErr(false) }
      } catch { if (!stopped) setErr(true) }
    }
    tick()
    const timer = setInterval(tick, SYS_POLL_MS)
    return () => { stopped = true; clearInterval(timer) }
  }, [ctx, on])
  return [data, err]
}

/* The ranked top-consumers list. `GET /sys/apps` already ranks heaviest-first
   and sums the totals, so this renders the backend's own answer rather than
   re-sorting it here. The System tab's ctx is the `/sys` adapter, so the call
   below (`/apps`) addresses `/sys/apps`. Polled a little slower than /stats:
   each reading asks the engine once per installed app, so it does not need to
   move every 3 s. */
function useSysApps(ctx, on) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(false)
  useEffect(() => {
    if (!on) return undefined
    let stopped = false
    const tick = async () => {
      try {
        const r = await ctx.rest('/apps', { timeoutMs: 12000 })
        if (!stopped) { setData(r || {}); setErr(false) }
      } catch { if (!stopped) setErr(true) }
    }
    tick()
    // The same cadence as the Apps tab: /sys/apps costs one engine call per
    // installed app, and usage does not move fast enough to justify a hotter
    // poll — a 6s interval here would out-poll the route it reads.
    const timer = setInterval(tick, 15000)
    return () => { stopped = true; clearInterval(timer) }
  }, [ctx, on])
  return [data, err]
}

/* One set of thresholds for both homes, so the chip and the card can never
   disagree about what counts as busy. */
function meterTone(pct) {
  const v = Number(pct) || 0
  if (v >= 90) return 'bad'
  if (v >= 75) return 'warn'
  return null
}

function MiniMeter({ label, pct, text, tone }) {
  const v = Math.max(0, Math.min(100, Number(pct) || 0))
  return jsxs('div', { className: 'ah-mm-meter', children: [
    jsxs('div', { className: 'ah-mm-top', children: [
      jsx('span', { children: label }),
      jsx('b', { children: text == null ? Math.round(v) + '%' : text }),
    ] }),
    jsx('div', { className: 'ah-mm-track', children:
      jsx('div', { className: 'ah-mm-fill', 'data-hot': tone === 'bad',
        'data-warn': tone === 'warn', style: { width: v + '%' } }) }),
  ] })
}

/* The compact readout: the popover under the chip, and the System tab when it
   is minimised. */
function MiniCard({ d, err, onOpen }) {
  if (err) {
    return jsx('div', { className: 'ah-mm-off', children:
      'No reading — the Athena backend is not answering.' })
  }
  if (!d) return jsx('div', { className: 'ah-mm-off', children: 'Reading this machine…' })
  const gpu = d.gpu
  const cpu = d.cpu || {}
  const mem = d.mem || {}
  const disks = d.disks || []
  const net = d.net || { nics: [] }
  const top = (d.top || []).slice(0, 5)
  const vramPct = gpu && gpu.mem_total_mib ? (gpu.mem_used_mib / gpu.mem_total_mib) * 100 : 0
  return jsxs('div', { children: [
    jsx(MiniMeter, {
      label: 'CPU' + (cpu.cores ? ' · ' + cpu.cores + ' cores' : '') +
        (cpu.freq_mhz ? ' · ' + (cpu.freq_mhz / 1000).toFixed(1) + ' GHz' : ''),
      pct: cpu.percent, tone: meterTone(cpu.percent) }),
    jsx(MiniMeter, { label: 'Memory', pct: mem.percent,
      text: mem.total_gb ? mem.used_gb + '/' + mem.total_gb + ' GB' : null,
      tone: meterTone(mem.percent) }),
    gpu ? jsx(MiniMeter, {
      label: 'GPU' + (gpu.temp_c != null ? ' · ' + Math.round(gpu.temp_c) + '°C' : ''),
      pct: gpu.util, tone: gpu.temp_c >= 83 ? 'bad' : meterTone(gpu.util) }) : null,
    gpu && gpu.mem_total_mib ? jsx(MiniMeter, {
      label: 'VRAM', pct: vramPct,
      text: (gpu.mem_used_mib / 1024).toFixed(1) + '/' +
            (gpu.mem_total_mib / 1024).toFixed(1) + ' GB',
      tone: meterTone(vramPct) }) : null,

    disks.length ? jsx('div', { className: 'ah-mm-group',
      children: disks.length > 1 ? 'Drives' : 'Drive' }) : null,
    disks.map((dk) => jsx(MiniMeter, { key: dk.mount, label: 'Disk ' + dk.mount,
      pct: dk.percent, text: dk.used_gb + '/' + dk.total_gb + ' GB',
      tone: meterTone(dk.percent) })),

    jsxs('div', { className: 'ah-mm-row', children: [
      jsx('span', { children: '↓ ' + fmtBps(d.net && d.net.down_bps) }),
      jsx('span', { children: '↑ ' + fmtBps(d.net && d.net.up_bps) }),
      jsx('b', { children: 'up ' + fmtUptime(d.boot_epoch) }),
    ] }),
    // What has moved since boot, and on which adapter. A live rate says the link
    // is busy; only a total says whether the download finished.
    net.nics.length ? jsxs('div', { className: 'ah-mm-row', children: [
      jsx('span', { children: 'Network · ' + (net.nics[0].name || 'adapter') }),
      jsx('b', { children: bytesLabel(net.recv_mb * 1048576) + ' ↓  ' +
                        bytesLabel(net.sent_mb * 1048576) + ' ↑' }),
    ] }) : null,

    // The shape of the last few minutes: the very same NetSpark the full
    // monitor draws, fed the backend's own /sys/stats history (a 120-sample
    // window). Rendered from the sample this card already polls, so there is no
    // second timer and no buffer of our own. It returns null for too few samples.
    jsx(AthenaSystem.NetSpark, { history: net.history }),

    top.length ? jsx('div', { className: 'ah-mm-group', children: 'Heaviest processes' }) : null,
    top.map((p) => jsxs('div', { className: 'ah-mm-row', children: [
      jsx('span', { children: p.name }),
      jsx('b', { children: p.mem + '%' }),
    ] }, p.name)),

    onOpen ? jsx('div', { className: 'ah-mm-foot', children:
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: onOpen,
        children: 'Open the full monitor' }) }) : null,
  ] })
}

/* The deep monitor is a window, not a tab. It is the same reading the status
   chip carries, taken further, so it exists only when it has been asked for —
   from the expanded mini monitor — and it closes like any other pane. */
const SYSTEM_WINDOW_ID = 'athena-system'

function openSystemWindow() {
  if (!athenaCtx || !hasWorkspaceApi()) return
  try {
    systemClose = host.openWorkspace(SYSTEM_WINDOW_ID, {
      title: 'System',
      minWidth: '22rem',
      dock: { pane: 'workspace', pos: 'right' },
      onClose: () => { systemClose = null },
      render: () => jsx(SystemWindow, { ctx: athenaCtx, full: systemFullFactory }),
    })
  } catch { systemClose = null }
}

/* The minimised form, in the status bar beside the music widget. */
function MiniMonitor({ ctx }) {
  const on = useValue(sysBar)
  const [open, setOpen] = useState(false)
  const [d, err] = useSysStats(ctx, on)
  // A desktop without the window API has nowhere to put the deep view, so the
  // button that would open it is not offered at all.
  const canOpenSystem = hasWorkspaceApi()
  if (!on) return null
  const pct = (x) => (x == null ? '·' : Math.round(Number(x) || 0) + '%')
  return jsx('div', { className: 'ah-mm-bar', children:
    jsxs(Popover, { open, onOpenChange: setOpen, children: [
      jsx(PopoverTrigger, { asChild: true, children:
        jsxs(Button, { variant: 'ghost', size: 'micro', 'aria-label': 'System monitor',
          children: [
            jsxs('span', { className: 'ah-mm-chip', children: [
              jsx('span', { children: 'CPU' }),
              jsx('b', { children: d && d.cpu ? pct(d.cpu.percent) : '·' }),
            ] }),
            jsx('span', { className: 'ah-mm-sep', children: '·' }),
            jsxs('span', { className: 'ah-mm-chip', children: [
              jsx('span', { children: 'RAM' }),
              jsx('b', { children: d && d.mem ? pct(d.mem.percent) : '·' }),
            ] }),
            d && d.gpu ? jsx('span', { className: 'ah-mm-sep', children: '·' }) : null,
            d && d.gpu ? jsxs('span', { className: 'ah-mm-chip', children: [
              jsx('span', { children: 'GPU' }),
              jsx('b', { children: pct(d.gpu.util) }),
            ] }) : null,
          ] }) }),
      jsx(PopoverContent, { side: 'top', align: 'end', className: 'ah-mm-panel',
        onFocusOutside: (e) => e.preventDefault(), children:
        jsxs('div', { children: [
          jsx('div', { className: 'ah-mm-head', children: [
            jsx(StatusDot, { tone: err ? 'bad' : 'good' }),
            jsx('span', { children: 'System monitor' }),
          ] }),
          jsx(MiniCard, { d, err, onOpen: canOpenSystem ? openSystemWindow : null }),
        ] }) }),
    ] })
  })
}

/* The System tab's ranked list of what is actually eating memory. The backend
   already orders it heaviest-first and names the single heaviest running app, so
   this is a readout of its answer, not a sort of its own. */
function TopConsumers({ ctx }) {
  const [d, err] = useSysApps(ctx, true)
  const apps = (d && d.apps) || []
  const running = apps.filter((a) => a.running)
  const totals = (d && d.totals) || {}
  const topId = d && d.top ? d.top.id : null
  const metric = (row, kind) => {
    const node = row && row.metrics && row.metrics[kind]
    return node && node.formatted ? node.formatted : null
  }
  if (err) {
    return jsx('div', { className: 'ah-sub', children:
      'No reading — the Athena backend is not answering.' })
  }
  if (!d) return jsx('div', { className: 'ah-sub', children: 'Reading app usage…' })
  // A table of zeros would answer a question nobody asked: when nothing is
  // running there is no usage to rank, so say exactly that.
  if (!running.length) {
    return jsxs('div', { children: [
      jsx('div', { className: 'ah-secthead', children: 'Top consumers' }),
      jsx('div', { className: 'ah-sub', children:
        'No app is running right now, so nothing is using memory. Launch one and it '
        + 'appears here, heaviest first.' }),
    ] })
  }
  return jsxs('div', { children: [
    jsx('div', { className: 'ah-secthead', children: 'Top consumers' }),
    jsx('div', { className: 'ah-sub', style: { marginBottom: 4 }, children:
      running.length + (running.length === 1 ? ' app running' : ' apps running')
      + ' · RAM ' + fmtBytes(totals.ram_bytes || 0)
      + ' · VRAM ' + fmtBytes(totals.vram_bytes || 0)
      + (topId ? ' · heaviest: ' + topId : '') }),
    apps.map((a) => {
      const isTop = a.id === topId
      return jsxs('div', { className: 'ah-kv', key: a.id, children: [
        jsx('span', { children: (isTop ? '▲ ' : '') + a.id
          + (a.running ? '' : ' (stopped)') }),
        jsx('span', { className: isTop ? 'ah-ok' : '', children: a.running
          ? 'RAM ' + (metric(a, 'ram') || '—') + ' · VRAM ' + (metric(a, 'vram') || '—')
          : 'not running' }),
      ] }, a.id)
    }),
  ] })
}

/* The deep view, in its own window: the ranked consumers plus the ported panel,
   which polls on its own. `full` is a factory the merged half registers, so the
   ported panel is only mounted while this window is actually open. */
function SystemWindow({ ctx, full }) {
  const bar = useValue(sysBar)
  return jsxs('div', { className: 'ah-systab', children: [
    jsxs('div', { className: 'ah-systab-bar', children: [
      jsx(StatusDot, { tone: 'good' }),
      jsx('span', { className: 'ah-sub', children:
        'Live, from this machine and from the engine for anything it is running.' }),
      jsx('span', { style: { marginLeft: 'auto' } }),
      jsx(Button, { size: 'micro', variant: 'ghost',
        onClick: () => setSysBar(ctx, !bar),
        children: bar ? 'Hide from status bar' : 'Show in status bar' }),
    ] }),
    jsxs('div', { className: 'ah-systab-full', children: [
      jsx(NeedsYou, { ctx }),
      jsx(TopConsumers, { ctx }),
      full ? full(ctx) : null,
      jsx(WeightShelf, { ctx }),
      jsx(GpuCostCard, { ctx }),
    ] }),
  ] })
}

/* ------------------------------------------------------------------ runs */

// 20261012-192005 -> 2026-10-12 19:20. The run's folder name is its timestamp,
// which is the only clock in the record worth trusting.
function runStamp(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(String(id || ''))
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(id || '')
}

function bytesLabel(n) {
  const v = Number(n) || 0
  if (v >= 1048576) return (v / 1048576).toFixed(1) + ' MB'
  if (v >= 10240) return Math.round(v / 1024) + ' KB'
  if (v >= 1024) return (v / 1024).toFixed(1) + ' KB'
  return v + ' B'
}

function runWords(intent) { return String(intent || '').replace(/_/g, ' ') }

// The thumbnail routes the ported file browser already uses, so a picture of a
// result costs one cached request instead of new plumbing.
const RUN_THUMB = { image: '/files/thumb', video: '/files/videothumb', model: '/files/modelthumb' }

/*
  What the agent has actually run here, as the backend recorded it: what was
  asked for, what went in, what came out, and which capability did it. Written by
  the call itself, so it is a history rather than this session's transcript — it
  survives a restart, and it is how someone learns what the machine can do.
*/
/*
  A repeat, in a sentence. Pure and hookless for the same reason the other
  renderers are: every state a person can be shown — waiting, paused, and the one
  that matters most, a job that is gone from the store and will never fire — has
  to be assertable without a network or a cron daemon.
*/
function nextWords(iso) {
  const m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/)
  return m ? `${m[3]}/${m[2]} ${m[4]}:${m[5]}` : String(iso || '')
}

const REPEAT_STATE = { waiting: 'will run', paused: 'paused', gone: 'will not run' }

function repeatPanel(data, opts) {
  const o = opts || {}
  const rows = ((data && data.schedules) || []).slice()
  if (data && data.hermes_cron === false) {
    return { rows: [], head: 'Repeats', notes: [],
             empty: 'Nothing can be set to run on its own here — this build of ' +
                    'Hermes has no cron store for Athena to put a schedule in.' }
  }
  if (!rows.length) {
    return { rows: [], head: 'Repeats', notes: [],
             empty: 'Nothing runs on its own yet. Ask me to run something every ' +
                    'morning — an upscale, a batch of images — and it appears here.' }
  }
  return {
    head: `${rows.length} repeat${rows.length === 1 ? '' : 's'}`,
    empty: '',
    notes: [data && data.note].filter(Boolean),
    rows: rows.map((s) => ({
      id: s.id,
      app: String(s.app || '').replace(/\.git$/, ''),
      label: s.name || runWords(s.intent) || s.capability || '',
      when: String(s.schedule || '').trim(),
      next: nextWords(s.next_run),
      state: s.state || 'waiting',
      stateWords: REPEAT_STATE[s.state] || String(s.state || ''),
      runs: (s.runs && s.runs.runs) || 0,
      note: s.note || '',
      canStop: o.onStop !== false,
    })),
  }
}

function RunsTab({ ctx }) {
  const [runs, setRuns] = useState(null)
  const [sched, setSched] = useState(null)
  const [err, setErr] = useState('')
  const [open, setOpen] = useState('')
  const [busy, setBusy] = useState(false)
  const [stopping, setStopping] = useState('')
  const [note, setNote] = useState('')
  const [thumbs, setThumbs] = useState({})
  const asked = useRef({})

  const load = useCallback(async (quiet) => {
    if (!quiet) setBusy(true)
    try {
      const r = await ctx.rest('/runs', { timeoutMs: 30000 })
      setRuns((r && r.runs) || [])
      setErr('')
    } catch (e) {
      setErr(String((e && e.message) || e))
    }
    try {
      setSched(await ctx.rest('/schedules', { timeoutMs: 30000 }))
    } catch (e) {
      setSched(null)
    }
    setBusy(false)
  }, [ctx])

  // Stopping is the one thing the panel can do to a schedule, and it says what
  // it did rather than making the row vanish and leaving the person wondering.
  const stopRepeat = useCallback(async (id) => {
    setStopping(id)
    try {
      const r = await ctx.rest('/schedules/' + encodeURIComponent(id),
                               { method: 'DELETE', timeoutMs: 30000 })
      // "Stopped — it will not run again" is a claim about the backend; a refusal answering
      // 200 with `ok: false` would have made the panel say it about a schedule still running.
      setNote(actionFailure('Stopping it', r) || 'Stopped — it will not run again.')
    } catch (e) {
      setNote('Could not stop it: ' + String((e && e.message) || e))
    }
    setStopping('')
    load(true)
  }, [ctx, load])

  useEffect(() => { load() }, [load])

  // Only the run that is open asks for pictures, and each file asks once: a
  // history full of heavy results must not fire a request per row.
  useEffect(() => {
    if (!open || !runs) return
    const run = runs.find((r) => r.id === open)
    if (!run) return
    ;(run.files || []).filter((f) => RUN_THUMB[f.kind]).slice(0, 8).forEach((f) => {
      if (asked.current[f.path]) return
      asked.current[f.path] = 1
      ctx.rest(RUN_THUMB[f.kind], { method: 'POST', body: { path: f.path, size: 320 }, timeoutMs: 30000 })
        .then((r) => setThumbs((m) => ({ ...m, [f.path]: (r && r.data_url) || '' })))
        .catch(() => setThumbs((m) => ({ ...m, [f.path]: '' })))
    })
  }, [open, runs, ctx])

  if (runs === null) {
    return jsx('div', { className: 'ah-empty', children: err
      ? 'Could not read the run history: ' + err
      : 'Reading the run history…' })
  }
  const panel = repeatPanel(sched, {})
  const repeats = jsxs('div', { className: 'ah-repeats', children: [
    jsxs('div', { className: 'ah-row', children: [
      jsx('span', { className: 'ah-sub', children: panel.empty ? 'Repeats' : panel.head }),
      jsx('span', { className: 'ah-grow' }),
    ] }),
    panel.empty ? jsx('div', { className: 'ah-empty', children: panel.empty }) : null,
    panel.rows.map((s) => jsxs('div', { className: 'ah-card ah-repeat', key: s.id, children: [
      jsxs('div', { className: 'ah-runrow', children: [
        jsx('span', { className: 'ah-runapp', children: s.app }),
        jsx('span', { className: 'ah-runintent', children: s.label }),
        jsx('span', { className: 'ah-grow' }),
        jsx('span', { className: 'ah-chip' + (s.state === 'waiting' ? ' ah-running' : ''),
                      children: s.stateWords }),
        jsx('span', { className: 'ah-sub', children:
          s.runs ? `${s.runs} run${s.runs === 1 ? '' : 's'}` : 'not run yet' }),
      ] }),
      jsxs('div', { className: 'ah-repeatwhen', children: [
        jsx('span', { children: s.when }),
        s.next ? jsx('span', { className: 'ah-sub', children: ' · next ' + s.next }) : null,
      ] }),
      s.note ? jsx('div', { className: 'ah-sub', children: s.note }) : null,
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => stopRepeat(s.id),
                    disabled: !s.canStop || stopping === s.id,
                    children: stopping === s.id ? 'Stopping…' : 'Stop' }),
    ] })),
    panel.notes.map((n) => jsx('div', { className: 'ah-sub', children: n })),
    note ? jsx('div', { className: 'ah-sub', children: note }) : null,
  ] })

  if (!runs.length) {
    return jsxs('div', { className: 'ah-runs', children: [repeats,
      jsxs('div', { className: 'ah-empty', children: [
        'Nothing has run yet.',
        jsx('div', { className: 'ah-sub', style: { marginTop: 6 }, children:
          'Ask me for something one of these apps does — remove a background, ' +
          'generate an image or a song — and it shows up here: what went in, what ' +
          'came out, and how it was done.' }),
      ] }) ] })
  }

  return jsxs('div', { className: 'ah-runs', children: [repeats,
    jsxs('div', { className: 'ah-row', children: [
      jsx('span', { className: 'ah-sub', children:
        `${runs.length} run${runs.length === 1 ? '' : 's'}, newest first` }),
      jsx('span', { style: { marginLeft: 'auto' } }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: load,
                    children: busy ? 'Refreshing…' : 'Refresh' }),
    ] }),
    runs.map((run) => jsx(RunsRow, {
      key: run.id,
      run,
      isOpen: open === run.id,
      thumbs,
      onToggle: () => setOpen(open === run.id ? '' : run.id),
    })),
  ] })
}

// One recorded run. Kept free of state so it can be rendered with real data
// (the tab's own state is only the network read), and so the row the user reads
// is the row the tests read.
function RunsRow({ run, isOpen, thumbs, onToggle }) {
  const files = run.files || []
  const marks = thumbs || {}
  return jsxs('div', { className: 'ah-card ah-run', children: [
    jsxs('div', { className: 'ah-runrow', title: run.dir, onClick: onToggle, children: [
      jsx('span', { className: 'ah-runapp', children: String(run.app).replace(/\.git$/, '') }),
      jsx('span', { className: 'ah-runintent', children: runWords(run.intent) }),
      jsx('span', { className: 'ah-grow' }),
      jsx('span', { className: 'ah-chip' + (run.state === 'running' ? ' ah-running' : ''),
                    children: run.state === 'running' ? 'running'
                      : run.state === 'failed' ? 'failed'
                      : `${files.length} file${files.length === 1 ? '' : 's'}` }),
      jsx('span', { className: 'ah-sub', children: runStamp(run.when) }),
    ] }),
    isOpen ? jsxs('div', { className: 'ah-rundetail', children: [
      jsxs('div', { className: 'ah-kv', children: [
        jsx('span', { className: 'ah-sub', children: 'capability' }),
        jsx('span', { className: 'ah-mono', children: run.capability || '—' }),
      ] }),
      jsxs('div', { className: 'ah-kv', children: [
        jsx('span', { className: 'ah-sub', children: 'asked for' }),
        jsx('span', { children: runWords(run.intent) || '—' }),
      ] }),
      run.state === 'running' ? jsx('div', { className: 'ah-sub', children:
        'still going — the record updates as it finishes' }) : null,
      run.error ? jsx('div', { className: 'ah-sub', children: 'failed: ' + run.error }) : null,
      (run.inputs || []).length ? jsxs('div', { className: 'ah-kv', children: [
        jsx('span', { className: 'ah-sub', children: 'inputs' }),
        jsx('span', { className: 'ah-mono', title: (run.inputs || []).join('\n'),
                      children: (run.inputs || []).map((p) =>
                        String(p).split(/[\\/]/).pop()).join(', ') }),
      ] }) : null,
      jsx('div', { className: 'ah-runfiles', children: files.map((f, i) => {
        const t = marks[f.path]
        const mine = run.likely_output && f.path === run.likely_output
        return jsxs('div', { className: 'ah-runfile' + (mine ? ' mine' : ''), title: f.path, children: [
          t ? jsx('img', { className: 'ah-thumb', src: t, alt: f.name }) : null,
          jsxs('div', { className: 'ah-runfilemeta', children: [
            jsx('span', { className: 'ah-runfilename', children: f.name }),
            jsx('span', { className: 'ah-sub', children:
              `${f.kind} · ${bytesLabel(f.bytes)}${mine ? ' · this run' : ''}` }),
          ] }),
          jsx(Button, { size: 'micro', variant: 'ghost',
            onClick: () => { try { navigator.clipboard.writeText(f.path) } catch (e) {} },
            children: 'Copy path' }),
        ] }, f.path + i)
      }) }),
    ] }) : null,
  ] })
}

/* ---------------------------------------------------------------- shell */

/* --------------------------------------------- sending a file to an app
 *
 * The other end of "what can this app do". A person has a file in front of them
 * and wants to know who will take it, so the browser's own right-click menu
 * hands the path to this panel — through one global function, because the ported
 * browser lives in its own scope and cannot call into this half.
 *
 * The panel does not decide anything itself: `/accepts` reads the manifests and
 * answers, and what it says about the apps that would *not* take the file is
 * shown too, because "nothing here takes a .wav" is a real answer and an empty
 * list is not. Handing the file over starts a job, so twenty minutes of
 * generation is a job id with a log and a pace line rather than a frozen button.
 */
const KIND_WORDS = {
  image: 'an image', video: 'a video', audio: 'an audio file',
  model3d: 'a 3D model', archive: 'an archive', data: 'a data file',
  text: 'text', file: 'a file', other: 'a file',
}

function kindWords(kind) {
  const k = String(kind || '')
  return KIND_WORDS[k] || (k ? 'a ' + k + ' file' : 'a file')
}

/* Every state this panel can be in, as data. Hookless on purpose: the harness
   cannot drive useState, so the wording is reached by handing this function the
   exact JSON the route sends. */
function acceptsPanel(d, opts) {
  const o = opts || {}
  if (!d) {
    return {
      state: 'idle', rows: [], notes: [],
      head: 'Right-click a file and choose “Send to an app…” — Athena will say which app here would take it.',
    }
  }
  if (d.error) return { state: 'refused', head: String(d.error), rows: [], notes: [] }
  const name = d.name || o.name || 'this file'
  const kind = d.kind || 'file'
  const fits = d.fits || []
  const refused = d.refused || []
  const unprobed = d.unprobed || []
  const notes = refused.map((r) => `${r.app_name || r.app} — ${r.reason}`)
  if (unprobed.length) {
    notes.push(unprobed.length === 1
      ? `${unprobed[0]} has not said what it can do yet — that is written the first time an app is asked, and it has to be running for that.`
      : `${unprobed.length} apps here have not said what they can do yet (${unprobed.join(', ')}) — that is written the first time an app is asked, and it has to be running for that.`)
  }
  if (!fits.length) {
    return {
      state: 'none', kind, name, rows: [], notes,
      head: (refused.length || unprobed.length)
        ? `Nothing installed here would take ${name} — it is ${kindWords(kind)}.`
        : 'Nothing installed here takes a file at all yet.',
    }
  }
  const rows = fits.map((r) => ({
    app: r.app, app_name: r.app_name || r.app, capability: r.capability,
    intent: r.intent, running: !!r.running, multi: !!r.multi,
    // The list warning belongs on both branches: a Files input that wants many
    // files is exactly the case where one path handed over as a string throws.
    line: (r.tier === 'exact'
      ? `takes ${kindWords(r.wants)} — the file goes in “${r.input_name || 'the file input'}”`
      : 'takes any file') + (r.multi ? ', as a list' : ''),
    note: r.running ? 'running now' : 'not running — it will be started for this',
  }))
  return {
    state: 'ready', kind, name, rows, notes,
    head: fits.length === 1
      ? `${name} — one app here would take it.`
      : `${name} — ${fits.length} apps here would take it.`,
  }
}

/* What a run made, and who would take it next. Pure for the same reason as the
   rest: the harness cannot drive useState, so the wording is reached by handing
   this the job snapshot the route sends and the routing answer for its first
   file. A chain that stopped short is its own state — a stop is not a failure of
   the panel, but saying nothing about it would be. */
function madePanel(snap, d, opts) {
  const o = opts || {}
  if (!snap || !snap.state || snap.state === 'running') {
    return { state: 'working', head: 'the app is working — this bar will say what it made', rows: [], notes: [] }
  }
  const res = snap.result || {}
  const stages = res.stages || []
  const notes = stages.map((s) => {
    const made = (s.files || []).length
    const where = `stage ${s.stage} · ${s.app}${s.intent ? ' · ' + s.intent : ''}`
    return s.ok ? `${where} — ${made ? made + ' file(s)' : 'made nothing'}`
                : `${where} — ${s.error || 'failed'}`
  })
  if (snap.state !== 'done') {
    return { state: 'lost', head: `it ${snap.state || 'stopped'}`, rows: [], notes }
  }
  if (res.ok === false) {
    return {
      state: 'stopped',
      head: res.stopped_at
        ? `the chain stopped at stage ${res.stopped_at}: ${res.error || 'it could not go on'}`
        : `it did not finish: ${res.error || 'no reason was given'}`,
      rows: [], notes,
    }
  }
  const files = res.files || []
  if (!files.length) {
    return { state: 'empty', head: (stages.length > 1 ? 'the chain finished but the last app made nothing to hand on' : 'it finished and made no files'), rows: [], notes }
  }
  const view = acceptsPanel(d, o)
  const name = String(files[0]).split(/[\\/]/).pop()
  const head = stages.length > 1
    ? `the chain finished: ${stages.length} stages, and it made ${files.length} file(s)`
    : `${o.app || 'the app'} made ${files.length} file(s) — ${name}`
  return { state: 'made', head, files, name, rows: view.rows, notes: notes.concat(view.notes), next: view.head }
}

function useSendToApp(ctx) {
  const [file, setFile] = useState('')
  const [data, setData] = useState(null)
  const [sending, setSending] = useState('')
  const [job, setJob] = useState(null)
  const [snap, setSnap] = useState(null)
  const [next, setNext] = useState(null)

  const clear = () => { setFile(''); setData(null); setSending(''); setJob(null); setSnap(null); setNext(null) }

  // Installed while this panel is on screen, and taken away with it: the donor
  // menu item checks for it, so a path can only arrive while there is something
  // on screen to receive it.
  useEffect(() => {
    window.__athenaSendToApp = (p) => {
      setFile(String(p || '')); setData(null); setSending(''); setJob(null); setSnap(null); setNext(null)
    }
    return () => { try { window.__athenaSendToApp = null } catch { /* noop */ } }
  }, [])

  useEffect(() => {
    if (!file) { setData(null); return undefined }
    let live = true
    ctx.rest('/accepts', { method: 'POST', timeoutMs: 20000, body: { path: file } })
      .then((r) => { if (live) setData(r || null) })
      .catch((e) => { if (live) setData({ error: String((e && e.message) || e) }) })
    return () => { live = false }
  }, [file, ctx])

  useEffect(() => {
    if (!job || !job.id) return undefined
    let live = true
    let timer = null
    const tick = () => {
      ctx.rest(`/jobs/${encodeURIComponent(job.id)}`, { timeoutMs: 8000 })
        .then((r) => {
          if (!live) return
          setSnap(r || null)
          if (!r || !r.state || r.state === 'running') timer = setTimeout(tick, 2500)
        })
        .catch(() => { if (live) timer = setTimeout(tick, 5000) })
    }
    timer = setTimeout(tick, 1200)
    return () => { live = false; if (timer) clearTimeout(timer) }
  }, [job, ctx])

  // Only once the job is over, and only for the first thing it made: this is the
  // question "and now what?", asked of the same routing answer as before.
  useEffect(() => {
    const done = snap && snap.state && snap.state !== 'running'
    const files = (done && snap.result && snap.result.files) || []
    if (!done || !files.length || !(snap.result || {}).ok) { setNext(null); return undefined }
    let live = true
    ctx.rest('/accepts', { method: 'POST', timeoutMs: 20000, body: { path: files[0] } })
      .then((r) => { if (live) setNext(r || null) })
      .catch(() => { if (live) setNext(null) })
    return () => { live = false }
  }, [snap, ctx])

  // Handing the run's own output on: the next call is made with the file the last
  // stage wrote, which is the same route the first call used.
  const sendOn = (row) => {
    const files = (snap && snap.result && snap.result.files) || []
    if (!files.length) return
    setSending(row.app)
    ctx.rest(`/apps/${encodeURIComponent(row.app)}/call`, {
      method: 'POST', timeoutMs: 30000,
      body: { capability: row.capability, inputs: [files[0]] },
    })
      .then((r) => {
        if (r && r.ok && r.job) {
          setJob({ id: r.job, app: row.app_name || row.app, intent: r.intent || row.capability })
          setSnap(null); setNext(null)
        } else {
          setData({ error: (r && r.error) || 'the app refused the call' })
        }
      })
      .catch((e) => setData({ error: String((e && e.message) || e) }))
      .then(() => setSending(''))
  }

  const send = (row) => {
    setSending(row.app)
    ctx.rest(`/apps/${encodeURIComponent(row.app)}/call`, {
      method: 'POST', timeoutMs: 30000,
      body: { capability: row.capability, inputs: [file] },
    })
      .then((r) => {
        if (r && r.ok && r.job) {
          setSnap(null)
          setJob({ id: r.job, app: row.app_name || row.app, intent: r.intent || row.capability })
        } else {
          setData({ error: (r && r.error) || 'the app refused the call' })
        }
      })
      .catch((e) => setData({ error: String((e && e.message) || e) }))
      .then(() => setSending(''))
  }

  return { file, data, sending, job, snap, next, send, sendOn, clear }
}

function SendToApp({ ctx }) {
  const s = useSendToApp(ctx)
  const view = acceptsPanel(s.data, { name: s.file })
  if (!s.file) {
    return jsx('div', { className: 'ah-send idle', children: view.head })
  }
  const pace = s.snap && s.snap.state ? jobPace(s.snap) : null
  const done = s.snap && s.snap.state && s.snap.state !== 'running'
  const files = (s.snap && s.snap.result && s.snap.result.files) || []
  const made = madePanel(s.snap, s.next, { name: s.file, app: s.job && s.job.app })
  return jsxs('div', { className: 'ah-send', children: [
    jsxs('div', { className: 'ah-send-head', children: [
      jsx('span', { className: 'ah-send-what', title: s.file, children: view.head }),
      jsx('span', { className: 'ah-send-grow' }),
      jsx('button', { className: 'ah-btn', onClick: s.clear, children: 'Done' }),
    ] }),
    view.rows.length
      ? jsx('div', { className: 'ah-send-rows', children: view.rows.map((r) =>
          jsxs('div', { className: 'ah-send-row', children: [
            jsxs('div', { className: 'ah-send-info', children: [
              jsx('div', { className: 'ah-send-app', children: r.app_name }),
              jsx('div', { className: 'ah-send-line', children: r.line }),
              jsx('div', { className: 'ah-send-note', children: r.note }),
            ] }),
            jsx('button', {
              className: 'ah-btn primary',
              disabled: !!s.sending,
              onClick: () => s.send(r),
              children: s.sending === r.app ? 'Sending…' : 'Send',
            }),
          ] }, r.app)) })
      : null,
    view.notes.length
      ? jsx('div', { className: 'ah-send-notes', children: view.notes.map((n, i) =>
          jsx('div', { className: 'ah-send-refused', children: n }, i)) })
      : null,
    s.job
      ? jsxs('div', { className: 'ah-send-job', children: [
          jsx('span', { children: done
            ? `Handed to ${s.job.app} — ${pace && pace.text ? pace.text : 'finished'}`
            : `Handed to ${s.job.app}${pace && pace.text ? ' — ' + pace.text : ''}` }),
          files.length
            ? jsx('span', { className: 'ah-send-file', title: files.join('\n'), children: files[0] })
            : null,
          jsx('span', { className: 'ah-send-hint', children: 'the log is in Runs' }),
        ] })
      : null,
    (done && made.state !== 'working' && (made.rows.length || made.notes.length || made.state !== 'made'))
      ? jsxs('div', { className: 'ah-then', children: [
          jsxs('div', { className: 'ah-send-head', children: [
            jsx('span', { className: 'ah-send-what', children: made.state === 'made'
              ? `${made.name} — send it on, and this becomes a chain` : made.head }),
          ] }),
          made.rows.length
            ? jsx('div', { className: 'ah-send-rows', children: made.rows.map((r) =>
                jsxs('div', { className: 'ah-send-row', children: [
                  jsxs('div', { className: 'ah-send-info', children: [
                    jsx('div', { className: 'ah-send-app', children: r.app_name }),
                    jsx('div', { className: 'ah-send-line', children: r.line }),
                    jsx('div', { className: 'ah-send-note', children: r.note }),
                  ] }),
                  jsx('button', {
                    className: 'ah-btn primary',
                    disabled: !!s.sending,
                    onClick: () => s.sendOn(r),
                    children: s.sending === r.app ? 'Sending…' : 'Send on',
                  }),
                ] }, 'then-' + r.app)) })
            : jsx('div', { className: 'ah-send-refused', children: made.next || 'nothing installed here would take it' }),
          made.state === 'made' ? null
            : jsx('div', { className: 'ah-send-notes', children: made.notes.map((n, i) =>
                jsx('div', { className: 'ah-send-refused', children: n }, 'stage-' + i)) }),
        ] })
      : null,
  ] })
}

function Panel({ ctx }) {
  const [st, err, refresh] = useEngine(ctx)
  // Built once per ctx: the ported UIs take ctx in hook dependencies, so a
  // fresh object each render would re-fire their effects on every paint.
  const filesCtx = useMemo(() => subCtx(ctx, '/files', 'files'), [ctx])
  const t = useValue(tab)
  const tabs = shellTabs(useValue(termApp), useValue(activeJob), useValue(watchMachine))
  return jsxs('div', { className: 'ah-root', children: [
    jsx(Header, { ctx, st, err, refresh }),
    jsx('div', { className: 'ah-tabs', children: tabs.map(([k, label]) =>
      jsx('button', {
        className: 'ah-tabb' + (t === k ? ' active' : ''),
        onClick: () => tab.set(k),
        children: label,
      }, k)) }),
    jsxs('div', { className: 'ah-body', children: [
      t === 'apps' ? jsx(AppsTab, { ctx, st, refresh }) : null,
      t === 'store' ? jsx(StoreTab, { ctx, st, refresh }) : null,
      t === 'files' ? jsxs('div', { className: 'ah-files', children: [
        jsx(AthenaFiles.Explorer, { ctx: filesCtx }),
        jsx(SendToApp, { ctx }),
      ] }) : null,
      t === 'runs' ? jsx(RunsTab, { ctx }) : null,
      t === 'machines' ? jsx(MachinesTab, { ctx, st }) : null,
      t === 'viewer' ? jsx(Viewer, { ctx }) : null,
      t === 'terminal' ? jsx(TerminalTab, { ctx }) : null,
      t === 'settings' ? jsx(SettingsTab, { ctx, st, refresh }) : null,
      t === 'mscreen' ? jsx(MachineScreenTab, { ctx }) : null,
    ] }),
  ] })
}

function StatusChip({ ctx }) {
  const open = useValue(winOpen)
  // A desktop without the window API can only front the pane, so the label
  // must not promise a close it cannot perform.
  const canToggle = hasWorkspaceApi()
  const [ready, setReady] = useState(false)
  useEffect(() => {
    let live = true
    let timer = null
    // Same self-scheduling rule as the panel: while the engine is not serving,
    // the chip watches once a second so the dot appears when it really is up,
    // not up to eight seconds later.
    const tick = async () => {
      let up = false
      try { const s = await ctx.rest('/status', { timeoutMs: 8000 }); up = !!(s && s.ready) }
      catch { up = false }
      if (!live) return
      setReady(up)
      timer = setTimeout(tick, up ? CHIP_POLL_MS : STARTING_POLL_MS)
    }
    tick()
    return () => { live = false; if (timer) clearTimeout(timer) }
  }, [ctx])
  return jsx(Tip, { label: 'Athena \u2014 click to ' + ((open && canToggle) ? 'close' : 'open') + ' the window'
      + (ready ? ' \u00b7 engine running' : ' \u00b7 engine stopped'), children:
    jsx(Button, {
      variant: 'ghost', size: 'micro', 'aria-label': 'Athena',
      'aria-pressed': open ? 'true' : 'false',
      className: open ? 'ah-chip on' : 'ah-chip',
      onClick: () => toggleAthenaWindow(),
      children: jsx('span', { style: { fontSize: 11 }, children: 'Athena' + (ready ? ' ●' : '') }),
    }) })
}

let paneDispose = null
let viewerDispose = null

/* ------------------------------------------------- donor adapters
 *
 * Each ported UI was written as its own plugin: it calls `ctx.rest('/list')`
 * against its own route prefix and keeps its settings under bare keys in the
 * shared store. The adapter re-points both, so the ported code runs unchanged
 * while its routes and storage stay out of Athena's way.
 */
function subCtx(base, prefix, ns) {
  return {
    rest: (p, o) => base.rest(prefix + p, o),
    storage: {
      get: (k, d) => {
        try {
          const v = base.storage.get(ns + ':' + k, d)
          return v === undefined ? d : v
        } catch { return d }
      },
      set: (k, v) => { try { base.storage.set(ns + ':' + k, v) } catch { /* noop */ } },
    },
    os: base.os,
    host: base.host,
    i18n: base.i18n,
    register: (...a) => base.register(...a),
    onDispose: (f) => base.onDispose(f),
  }
}

/* The 2D canvas API parses colours itself and does not resolve CSS custom
   properties, so a themed canvas surface has to be read off the document
   rather than written as var(). Falls back to the literal if it is unset. */
function themeInk(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
    return v || fallback
  } catch { return fallback }
}

/* ===================================================== app file browser
 *
 * Ported from the standalone file browser, re-scoped to Athena's home. Its own
 * prefix, storage namespace and i18n id come from the adapter below, so the
 * body is wrapped to keep its `ID`/`CSS`/`activeJob` from colliding with
 * Athena's identically-named module-level bindings.
 */
const AthenaFiles = (() => {
const ID = 'athena'
const RENDER_CAP = 800
const collapsed = atom(true)
const clipboard = atom(null)
const pendingRename = atom(null)
const pendingDelete = atom(null)
const dirBump = atom(0)
const activeJob = atom(null)
const jobStatus = atom(null)
let jobResolve = null
let jobDoneFlag = false
const runJob = async (ctx, op, body, label) => {
  const s = await ctx.rest('/jobstart', { method: 'POST', timeoutMs: 30000, body: { op, ...body } })
  if (!s?.job_id) return s
  jobDoneFlag = false
  jobResolve = null
  jobStatus.set(null)
  activeJob.set({ id: s.job_id, op, label: label || op })
  return new Promise(resolve => {
    jobResolve = resolve
    setTimeout(() => { if (jobResolve === resolve) { jobResolve = null; resolve({ ok: false, error: 'job timeout' }) } }, 3600000)
  })
}
const cancelJob = ctx => {
  const j = activeJob.get()
  if (j) ctx.rest('/jobcancel', { method: 'POST', timeoutMs: 15000, body: { job_id: j.id } }).catch(() => {})
}
// Dual-pane registry: every mounted TabPane reports { cwd, visible } here so
// "copy/move to other pane" can find the visible peer without prop drilling.
const paneCwds = {}
const otherPaneCwd = (tabId, cwd) => {
  try {
    const hit = Object.entries(paneCwds).find(([id, v]) => String(id) !== String(tabId) && v && v.visible && v.cwd && v.cwd !== 'trash://' && v.cwd !== cwd)
    return hit ? hit[1].cwd : null
  } catch { return null }
}

function hasFbPaths(ev) {
  try { return Array.from(ev.dataTransfer?.types || []).includes('application/x-hermes-filebox-paths') } catch { return false }
}

function parentDir(p) {
  const d = String(p || '').replace(/[/\\][^/\\]+$/, '')
  return d || p
}
let paneCtx = null
let paneRegister = null
let paneDispose = null

function syncPane() {
  if (!paneCtx || !paneRegister) return
  const open = !collapsed.get()
  if (open && !paneDispose) {
    try { paneDispose = paneRegister() } catch { /* noop */ }
    try { if (host && typeof host.revealPane === 'function') host.revealPane('hermes-filebox:hermes-filebox-pane') } catch { /* noop */ }
  } else if (!open && paneDispose) {
    try { paneDispose() } catch { /* noop */ }
    paneDispose = null
  }
}

const EN = {
  explorer: 'Explorer',
  quickAccess: 'Quick access',
  thisPC: 'This PC',
  searchFiles: 'Search this folder…',
  searchDeep: (n, folder) => `🔍 ${n} found under ${folder} — double-click a folder to go there`,
  searchMore: 'Showing first results — refine to narrow',
  searchingSub: 'Searching subfolders…',
  back: 'Back', fwd: 'Forward', up: 'Up', refresh: 'Refresh', home: 'Home',
  newFolder: 'New folder', newFile: 'New file', create: 'Create', cancel: 'Cancel',
  showHidden: 'Show hidden files',
  pinFolder: 'Pin this folder', unpinFolder: 'Unpin this folder',
  reveal: 'Show in Explorer', copyPath: 'Copy path', openFile: 'Open',
  rename: 'Rename', save: 'Save',
  copy: 'Copy', cut: 'Cut', paste: 'Paste', duplicate: 'Duplicate',
  delete: 'Delete', confirmDelete: 'Confirm delete?',
  compress: 'Compress', extract: 'Extract here',
  openFailed: 'Could not open — revealed the folder instead.',
  items: n => `${n} item${n === 1 ? '' : 's'}`,
  showingFirst: (a, b) => `Showing ${a} of ${b} — refine search`,
  emptyFolder: 'Empty folder.',
  backendDown: 'Backend unreachable — restart Hermes to remount Athena.',
  sortBy: 'Sort',
  sortName: 'Name', sortSize: 'Size', sortDate: 'Modified', sortType: 'Type',
  preview: 'Preview', details: 'Details',
  closePreview: 'Close preview',
  collapse: 'Collapse panel', expand: 'Expand panel',
  binaryFile: 'Binary file — no text preview.',
  mediaBlocked: 'Preview blocked here — use Open instead.',
  bigModel: 'Model too large to preview — use Open instead.',
  resetView: 'Reset view',
  readFull: 'Read full file',
  editFile: 'Edit',
  showLess: 'Show less',
  updateTo: v => `Update to v${v}`,
  checkingUpdate: 'Checking for updates…',
  selectedN: (n, size) => `${n} selected · ${size}`,
  clear: 'Clear selection',
  dragResize: 'Drag to resize',
  recycleBin: 'Recycle Bin',
  restore: 'Restore',
  restoreFirst: 'Restore it from the Recycle Bin first.',
  emptyBin: 'Empty Recycle Bin',
  emptyBinTip: 'Permanently delete everything in the Recycle Bin',
  confirmEmpty: 'Click again to empty',
  origPath: 'Original location',
  deletedOn: 'Deleted',
  copyOutside: 'Copy outside Hermes',
  nothingToPaste: 'Nothing to paste — copy something first.',
  viewList: 'List view',
  viewGrid: 'Grid view',
  cName: 'Name',
  cSize: 'Size',
  cModified: 'Modified',
  cType: 'Type',
  folder: 'Folder',
  properties: 'Properties',
  location: 'Location',
  type: 'Type',
  kindFolder: 'Folder',
  size: 'Size',
  created: 'Created',
  modified: 'Modified',
  hidden: 'Hidden',
  calcSize: 'Calculate size',
  calcNote: 'Large folders are sampled at 100k files.',
  yes: 'Yes',
  no: 'No',
  editPath: 'Edit path',
  pathNotFound: 'Path not found',
  notFolder: 'Not a folder — selected it instead',
  expand: 'Expand',
  zoomIn: 'Zoom in',
  zoomOut: 'Zoom out',
  zoomReset: 'Reset zoom',
  searchContent: 'Search inside files',
  grepHits: (n, files) => `📄 ${n} matches in ${files} — click to open`,
  grepMore: 'showing first 100',
  noGrepHits: 'No matches in file contents',
  browseZip: 'Browse inside',
  closeZip: 'Back to folder',
  extractAll: 'Extract all here',
  openMember: 'Open',
  zipOpBlocked: 'Not available inside zips — extract first',
  jobCopy: n => `Copy ${n} item${n === 1 ? '' : 's'}`,
  jobMove: n => `Move ${n} item${n === 1 ? '' : 's'}`,
  jobZip: n => `Compress ${n} item${n === 1 ? '' : 's'}`,
  jobExtract: name => `Extract ${name}`,
  cancelJob: 'Cancel',
  dualPane: 'Split view (dual pane)',
  copyToOther: 'Copy to other pane',
  moveToOther: 'Move to other pane',
  noOtherPane: 'No other pane available',
}
const LOCALES = { en: EN }

const CSS = `
.hermes-fb{display:flex;height:100%;min-height:0;color:var(--ui-text-primary);position:relative}
.hermes-fb-nav{width:148px;flex-shrink:0;overflow-y:auto;padding:6px 4px;border-right:1px solid var(--ui-stroke-secondary)}
.hermes-fb-group{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--ui-text-quaternary);padding:6px 6px 2px}
.hermes-fb-navbtn{display:flex;align-items:center;gap:6px;width:100%;text-align:left;font-size:12px;color:var(--ui-text-secondary);background:none;border:0;border-radius:calc(var(--radius-scalar) * .25rem);padding:4px 6px;cursor:pointer;min-width:0}
.hermes-fb-navbtn:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.hermes-fb-navbtn[data-on=true]{background:var(--chrome-action-hover);color:var(--ui-accent)}
.hermes-fb-navbtn span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hermes-fb-drivebar{height:3px;border-radius:2px;background:var(--chrome-action-hover);margin:2px 6px 4px;overflow:hidden}
.hermes-fb-drivebar>div{height:100%;background:var(--ui-accent)}
.hermes-fb-main{flex:1;min-width:0;display:flex;flex-direction:column}
.hermes-fb-toolbar{display:flex;align-items:center;gap:2px;padding:4px 6px;flex-shrink:0}
.hermes-fb-tbtn{width:24px;height:24px;display:inline-flex;align-items:center;justify-content:center;font-size:13px;color:var(--ui-text-secondary);background:none;border:0;border-radius:calc(var(--radius-scalar) * .25rem);cursor:pointer;flex-shrink:0}
.hermes-fb-tbtn:hover:not(:disabled){background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.hermes-fb-tbtn:disabled{opacity:.3;cursor:default}
.hermes-fb-crumbs{display:flex;align-items:center;gap:0;flex:1;min-width:0;overflow-x:auto;font-size:12px;scrollbar-width:none}
.hermes-fb-crumbs::-webkit-scrollbar{display:none}
.hermes-fb-crumb{background:none;border:0;color:var(--ui-text-secondary);cursor:pointer;padding:3px 5px;border-radius:calc(var(--radius-scalar) * .25rem);white-space:nowrap;font-size:12px}
.hermes-fb-crumb:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.hermes-fb-crumb[data-on=true]{color:var(--ui-accent)}
.hermes-fb-searchrow{display:flex;gap:4px;padding:0 6px 4px;flex-shrink:0;align-items:center}
.hermes-fb-search{flex:1;min-width:0}
.hermes-fb-search input{width:100%}
.hermes-fb-sort{background:transparent;border:1px solid var(--ui-stroke-secondary);border-radius:calc(var(--radius-scalar) * .25rem);color:var(--ui-text-secondary);font-size:11px;height:24px;flex-shrink:0}
.hermes-fb-grid{flex:1;min-height:0;overflow-y:auto;padding:2px 6px;display:grid;gap:2px;align-content:start}
.hermes-fb-tile{display:flex;flex-direction:column;align-items:center;gap:2px;background:none;border:1px solid transparent;border-radius:calc(var(--radius-scalar) * .375rem);padding:8px 4px 6px;cursor:pointer;min-width:0;color:var(--ui-text-primary)}
.hermes-fb-tile:hover{background:var(--chrome-action-hover)}
.hermes-fb-tile[data-on=true]{border-color:var(--ui-accent);background:var(--chrome-action-hover)}
.hermes-fb-tileicon{color:var(--ui-text-tertiary);display:flex;align-items:center;justify-content:center;flex-shrink:0}
.hermes-fb-tile[data-dir=true] .hermes-fb-tileicon{color:var(--ui-yellow)}
.hermes-fb-tileimg{border-radius:calc(var(--radius-scalar) * .25rem);object-fit:cover;background:var(--chrome-action-hover)}
.hermes-fb-tilename{font-size:11px;line-height:14px;text-align:center;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word}
.hermes-fb-tilesub{font-size:10px;color:var(--ui-text-quaternary);white-space:nowrap}
.hermes-fb-status{display:flex;gap:8px;font-size:11px;color:var(--ui-text-tertiary);padding:4px 8px;border-top:1px solid var(--ui-stroke-secondary);flex-shrink:0;overflow:hidden;white-space:nowrap}
.hermes-fb-preview{border-top:1px solid var(--ui-stroke-secondary);max-height:44%;overflow-y:auto;padding:6px 8px;flex-shrink:0}
.hermes-fb-preview img{max-width:100%;border-radius:calc(var(--radius-scalar) * .375rem)}
.hermes-fb-preview pre{font-size:11px;line-height:15px;white-space:pre-wrap;word-break:break-word;color:var(--ui-text-secondary);margin:4px 0 0;font-family:inherit}
.hermes-fb-kv{display:flex;justify-content:space-between;font-size:11px;color:var(--ui-text-secondary);padding:1px 0}
.hermes-fb-kv b{color:var(--ui-text-primary);font-weight:600}
.hermes-fb-actions{display:flex;gap:4px;margin-top:6px;flex-wrap:wrap}
.hermes-fb-mkdir{display:flex;gap:4px;padding:4px 6px;flex-shrink:0}
.hermes-fb-mkdir input{flex:1;min-width:0;height:24px;font-size:12px;padding:0 6px;border-radius:calc(var(--radius-scalar) * .25rem);border:1px solid var(--ui-stroke-secondary);background:transparent;color:var(--ui-text-primary)}
.hermes-fb-error{font-size:11px;color:var(--ui-text-secondary);padding:8px}
.hermes-fb-tabs{display:flex;align-items:center;gap:2px;padding:4px 6px 0;flex-shrink:0;overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--ui-stroke-secondary)}
.hermes-fb-tab{display:inline-flex;align-items:center;gap:5px;max-width:150px;font-size:11px;color:var(--ui-text-secondary);background:none;border:1px solid transparent;border-bottom:0;border-radius:calc(var(--radius-scalar) * .375rem) 6px 0 0;padding:4px 6px;cursor:pointer;white-space:nowrap;flex-shrink:0}
.hermes-fb-tab:hover{color:var(--ui-text-primary)}
.hermes-fb-tab[data-on=true]{background:var(--chrome-action-hover);color:var(--ui-text-primary);border-color:var(--ui-stroke-secondary)}
.hermes-fb-tab span:first-child{overflow:hidden;text-overflow:ellipsis}
.hermes-fb-tabx{background:none;border:0;color:var(--ui-text-quaternary);cursor:pointer;font-size:13px;line-height:1;padding:0 2px;border-radius:3px}
.hermes-fb-tabx:hover{color:var(--ui-text-primary)}
.hermes-fb-grip{width:6px;cursor:col-resize;flex-shrink:0;border-right:1px solid var(--ui-stroke-secondary)}
.hermes-fb-grip:hover{background:var(--ui-accent)}
.hermes-fb-rail{width:26px;flex-shrink:0;display:flex;flex-direction:column;align-items:center;padding-top:6px;border-right:1px solid var(--ui-stroke-secondary)}
.hermes-fb-opsbar{display:flex;gap:4px;flex-wrap:wrap;align-items:center;padding:6px 8px;border-bottom:1px solid var(--ui-stroke-secondary)}
.hermes-fb-pvwrap{display:flex;flex-direction:column;flex-shrink:0;min-height:0;border-top:1px solid var(--ui-stroke-secondary)}
.hermes-fb-pvwrap .hermes-fb-preview{border-top:0;flex:1;min-height:0;max-height:none}
.hermes-fb-griph{height:8px;cursor:row-resize;flex-shrink:0;touch-action:none;position:relative}
.hermes-fb-griph::after{content:'';position:absolute;left:50%;top:3px;width:36px;height:2px;margin-left:-18px;border-radius:1px;background:var(--ui-stroke-secondary)}
.hermes-fb-griph:hover{background:var(--chrome-action-hover)}
.hermes-fb-griph:hover::after{background:var(--ui-accent)}
.hermes-fb-tile[data-drop=true]{border-color:var(--ui-accent);background:var(--chrome-action-hover)}
.hermes-fb-list{flex:1;min-height:0;overflow-y:auto;padding:2px 4px;display:flex;flex-direction:column}
.hermes-fb-lhead,.hermes-fb-lrow{display:grid;grid-template-columns:minmax(0,1fr) 84px 128px 72px;gap:8px;align-items:center;padding:3px 8px;font-size:12px}
.hermes-fb-lhead{color:var(--ui-text-tertiary);font-size:10px;text-transform:uppercase;letter-spacing:.05em}
.hermes-fb-lhbtn{background:none;border:0;padding:0;font:inherit;color:inherit;text-transform:inherit;letter-spacing:inherit;cursor:pointer;text-align:left}
.hermes-fb-lhbtn:hover{color:var(--ui-text-primary)}
.hermes-fb-lhbtn[data-on=true]{color:var(--ui-accent)}
.hermes-fb-lrow{border-radius:calc(var(--radius-scalar) * .25rem);cursor:pointer;color:var(--ui-text-primary)}
.hermes-fb-lrow:hover{background:var(--chrome-action-hover)}
.hermes-fb-lrow[data-on=true]{background:var(--chrome-action-hover)}
.hermes-fb-lrow[data-drop=true]{outline:1px solid var(--ui-accent)}
.hermes-fb-lname{display:flex;gap:8px;align-items:center;min-width:0}
.hermes-fb-lname span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hermes-fb-lnum{text-align:right;color:var(--ui-text-secondary)}
.hermes-fb-list:focus,.hermes-fb-grid:focus{outline:none}
.hermes-fb-lb{position:absolute;inset:0;z-index:30;background:rgba(0,0,0,.9);display:flex;flex-direction:column}
.hermes-fb-lbhead{display:flex;align-items:center;gap:6px;padding:6px 8px;font-size:12px;color:var(--ui-text-secondary);flex-shrink:0}
.hermes-fb-lbhead .hermes-fb-tbtn{color:var(--ui-text-tertiary)}
.hermes-fb-lbbody{flex:1;min-height:0;overflow:auto;display:flex;align-items:center;justify-content:center;padding:8px}
.hermes-fb-lbbody img{border-radius:calc(var(--radius-scalar) * .25rem);object-fit:contain}
.hermes-fb-addr{flex:1;min-width:0;height:24px;font-size:12px;padding:0 6px;border-radius:calc(var(--radius-scalar) * .25rem);border:1px solid var(--ui-accent);background:transparent;color:var(--ui-text-primary)}
`

const CODE_EXTS = new Set(['.py', '.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs', '.json', '.yaml', '.yml', '.toml', '.md', '.txt', '.log', '.ini', '.cfg', '.css', '.html', '.xml', '.csv', '.ps1', '.bat', '.sh', '.java', '.c', '.cpp', '.h', '.rs', '.go', '.kt', '.sql', '.vue'])
const IMG_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.ico', '.tif', '.tiff'])
const VID_EXTS = new Set(['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.webm', '.m4v', '.ts'])
const AUD_EXTS = new Set(['.mp3', '.wav', '.flac', '.ogg', '.m4a', '.opus', '.wma'])
const MODEL_EXTS = new Set(['.stl', '.obj'])
const ZIP_EXTS = new Set(['.zip', '.7z', '.rar', '.tar', '.gz'])

function category(e) {
  if (e.is_dir) return 'dir'
  if (IMG_EXTS.has(e.ext)) return 'img'
  if (AUD_EXTS.has(e.ext)) return 'audio'
  if (VID_EXTS.has(e.ext)) return 'video'
  if (MODEL_EXTS.has(e.ext)) return 'model'
  if (ZIP_EXTS.has(e.ext)) return 'zip'
  if (CODE_EXTS.has(e.ext)) return 'code'
  if (e.ext === '.pdf') return 'pdf'
  return 'file'
}

function iconFor(cat) {
  switch (cat) {
    case 'dir': return 'folder'
    case 'img': case 'audio': case 'video': return 'file-media'
    case 'zip': return 'file-zip'
    case 'code': return 'file-code'
    case 'pdf': return 'file-pdf'
    default: return 'file'
  }
}

function fmtSize(b) {
  b = Number(b) || 0
  if (b >= 1073741824) return `${(b / 1073741824).toFixed(1)} GB`
  if (b >= 1048576) return `${(b / 1048576).toFixed(1)} MB`
  if (b >= 1024) return `${(b / 1024).toFixed(0)} KB`
  return `${b} B`
}

function fmtDate(epoch) {
  if (!epoch) return '—'
  const d = new Date(epoch * 1000)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  if (sameDay) return time
  return `${d.toLocaleDateString([], { month: 'numeric', day: 'numeric', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' })} ${time}`
}

function splitPath(path) {
  const parts = path.split(/[/\\]+/).filter(Boolean)
  if (/^[A-Za-z]:$/.test(parts[0])) parts[0] = `${parts[0]}\\`
  return parts
}

function thumbKind(ext) {
  if (IMG_EXTS.has(ext)) return 'img'
  if (VID_EXTS.has(ext)) return 'video'
  if (MODEL_EXTS.has(ext)) return 'model'
  return null
}

function Thumb({ ctx, entry, px, fallback }) {
  const kind = thumbKind(entry.ext)
  const ep = kind === 'video' ? '/videothumb' : kind === 'model' ? '/modelthumb' : '/thumb'
  const q = useQuery({
    queryKey: [ID, ep, entry.path, entry.mtime],
    queryFn: ({ signal }) => ctx.rest(ep, { method: 'POST', timeoutMs: 30000, signal, body: { path: entry.path, size: 256 } }),
    enabled: !!kind,
    staleTime: 300000,
    retry: false,
  })
  if (!kind || q.isError) return fallback || null
  if (!q.data?.data_url) return null
  return jsx('img', { className: 'hermes-fb-tileimg', src: q.data.data_url, alt: '', loading: 'lazy', style: { width: px, height: px } })
}

function fileUrl(path) {
  return 'file:///' + String(path || '').replace(/\\/g, '/').split('/').map(encodeURIComponent).join('/')
}

function fileDlUrl(path) {
  // Same-origin backend stream for <audio>/<video> when file:// is blocked.
  const base = (typeof window !== 'undefined' && window.location && window.location.origin) || ''
  return `${base}/api/plugins/athena/files/dl?path=${encodeURIComponent(path)}`
}

function parseModel(ext, bytes) {
  const capTris = 12000
  if (ext === '.obj') {
    const text = new TextDecoder().decode(bytes.slice(0, 32 * 1024 * 1024))
    const verts = []
    const out = []
    for (const line of text.split('\n')) {
      if (line[0] === 'v' && line[1] === ' ') {
        const p = line.split(/\s+/)
        verts.push([+p[1], +p[2], +p[3]])
      } else if (line[0] === 'f' && line[1] === ' ') {
        const idx = line.slice(1).trim().split(/\s+/).map(s => parseInt(s.split('/')[0], 10) - 1)
        for (let k = 1; k < idx.length - 1 && out.length < capTris * 9; k++) {
          for (const j of [idx[0], idx[k], idx[k + 1]]) {
            const v = verts[j]
            if (v && v.every(Number.isFinite)) out.push(v[0], v[1], v[2])
          }
        }
      }
      if (out.length >= capTris * 9) break
    }
    const n = Math.floor(out.length / 9)
    return n ? { tris: new Float32Array(out.slice(0, n * 9)), n } : null
  }
  if (bytes.length > 84) {
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    const count = dv.getUint32(80, true)
    if (count > 0 && count < 3000000 && 84 + count * 50 === bytes.length) {
      const stride = Math.max(1, Math.ceil(count / capTris))
      const out = []
      for (let i = 0; i < count; i += stride) {
        const o = 84 + i * 50 + 12
        for (let k = 0; k < 9; k++) out.push(dv.getFloat32(o + k * 4, true))
      }
      return { tris: new Float32Array(out), n: Math.floor(out.length / 9) }
    }
  }
  const text = new TextDecoder().decode(bytes.slice(0, 64 * 1024 * 1024))
  const re = /vertex\s+(\S+)\s+(\S+)\s+(\S+)/g
  const out = []
  let m
  while ((m = re.exec(text)) && out.length < capTris * 9) out.push(+m[1], +m[2], +m[3])
  const n = Math.floor(out.length / 9)
  return n ? { tris: new Float32Array(out.slice(0, n * 9)), n } : null
}

function drawModel(canvas, geo, rx, ry, zoom) {
  const W = canvas.width, H = canvas.height
  const g = canvas.getContext('2d')
  g.fillStyle = themeInk('--ui-bg-editor', '#141419')
  g.fillRect(0, 0, W, H)
  const { tris, n } = geo
  const b = geo.bounds
  const span = Math.max(b.x1 - b.x0, b.y1 - b.y0, b.z1 - b.z0, 1e-9)
  const s = Math.min(W, H) * 0.42 * zoom / span
  const ox = W / 2, oy = H / 2
  const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2, cz = (b.z0 + b.z1) / 2
  const c1 = Math.cos(ry), s1 = Math.sin(ry), c2 = Math.cos(rx), s2 = Math.sin(rx)
  const items = []
  for (let i = 0; i < n; i++) {
    const p = []
    for (let k = 0; k < 3; k++) {
      const X = tris[i * 9 + k * 3] - cx, Y = tris[i * 9 + k * 3 + 1] - cy, Z = tris[i * 9 + k * 3 + 2] - cz
      const x1 = X * c1 + Z * s1, z1 = -X * s1 + Z * c1
      const y1 = Y * c2 - z1 * s2, z2 = Y * s2 + z1 * c2
      p.push(ox + x1 * s, oy - y1 * s, z2)
    }
    const ex = p[3] - p[0], ey = p[4] - p[1], ez = p[5] - p[2]
    const fx = p[6] - p[0], fy = p[7] - p[1], fz = p[8] - p[2]
    const nx = ey * fz - ez * fy, ny = ez * fx - ex * fz, nz = ex * fy - ey * fx
    const ln = Math.sqrt(nx * nx + ny * ny + nz * nz) + 1e-9
    const shade = Math.abs((nx * 0.4 + ny * 0.5 + nz * 0.75) / ln / 1.05)
    items.push({ p, d: (p[2] + p[5] + p[8]) / 3, v: Math.round(45 + 175 * Math.min(1, 0.15 + 0.85 * shade)) })
  }
  items.sort((a, b) => a.d - b.d)
  for (const it of items) {
    const v = it.v
    g.fillStyle = `rgb(${v},${v},${Math.min(255, v + 12)})`
    g.beginPath()
    g.moveTo(it.p[0], it.p[1])
    g.lineTo(it.p[3], it.p[4])
    g.lineTo(it.p[6], it.p[7])
    g.closePath()
    g.fill()
  }
}

function ModelView({ ctx, t, entry }) {
  const ref = useRef(null)
  const [rot, setRot] = useState({ x: 0.5, y: 0.6, z: 1 })
  const q = useQuery({
    queryKey: [ID, 'raw', entry.path, entry.mtime],
    queryFn: ({ signal }) => ctx.rest('/raw', { method: 'POST', timeoutMs: 30000, signal, body: { path: entry.path, max_mb: 48 } }),
    enabled: entry.size < 96 * 1024 * 1024,
    staleTime: 300000,
    retry: false,
  })
  const geo = useMemo(() => {
    if (!q.data?.b64) return null
    try {
      const bin = Uint8Array.from(atob(q.data.b64), c => c.charCodeAt(0))
      const parsed = parseModel(entry.ext, bin)
      if (!parsed || !parsed.tris.every(Number.isFinite)) return { error: true }
      let x0 = 1 / 0, y0 = 1 / 0, z0 = 1 / 0, x1 = -1 / 0, y1 = -1 / 0, z1 = -1 / 0
      const tr = parsed.tris
      for (let i = 0; i < tr.length; i += 3) {
        const X = tr[i], Y = tr[i + 1], Z = tr[i + 2]
        if (X < x0) x0 = X; if (X > x1) x1 = X
        if (Y < y0) y0 = Y; if (Y > y1) y1 = Y
        if (Z < z0) z0 = Z; if (Z > z1) z1 = Z
      }
      return { ...parsed, bounds: { x0, y0, z0, x1, y1, z1 } }
    } catch { return { error: true } }
  }, [q.data])
  useEffect(() => {
    if (ref.current && geo && !geo.error) {
      try { drawModel(ref.current, geo, rot.x, rot.y, rot.z) } catch { /* noop */ }
    }
  }, [geo, rot])
  const startSpin = e => {
    e.preventDefault()
    const sx = e.clientX, sy = e.clientY
    const r0 = { ...rot }
    const move = ev => setRot(r => ({ ...r, x: r0.x + (ev.clientY - sy) * 0.01, y: r0.y + (ev.clientX - sx) * 0.01 }))
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }
  if (entry.size >= 96 * 1024 * 1024) return jsx('div', { className: 'hermes-fb-error', children: t('bigModel') })
  if (q.isError) return jsx('div', { className: 'hermes-fb-error', children: t('mediaBlocked') })
  if (!geo) return jsx(GlyphSpinner, { ariaLabel: entry.name })
  if (geo.error) return jsx('div', { className: 'hermes-fb-error', children: t('mediaBlocked') })
  return jsxs('div', { children: [
    jsx('canvas', { ref, width: 560, height: 340, onMouseDown: startSpin, style: { width: '100%', borderRadius: 6, marginTop: 4, cursor: 'grab', background: 'var(--ui-bg-editor)', touchAction: 'none' } }),
    jsxs('div', { className: 'hermes-fb-actions', children: [
      jsx('span', { className: 'hermes-fb-tilesub', children: `${geo.n} tris · drag to rotate` }),
      jsx('span', { style: { flex: 1 } }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => setRot(r => ({ ...r, z: Math.max(0.3, +(r.z / 1.25).toFixed(2)) })), children: '−' }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => setRot(r => ({ ...r, z: Math.min(5, +(r.z * 1.25).toFixed(2)) })), children: '+' }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => setRot({ x: 0.5, y: 0.6, z: 1 }), children: t('resetView') }),
    ] }),
  ] })
}

function TrashPreview({ ctx, t, entry, refetch, select }) {
  const [busy, setBusy] = useState(false)
  const doRestore = async () => {
    setBusy(true)
    try {
      const r = await ctx.rest('/trash-restore', { method: 'POST', timeoutMs: 60000, body: { id: entry.path } })
      if (!r?.ok) hostNotify(ctx, 'restore failed')
      select(null)
      refetch()
    } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
    setBusy(false)
  }
  return jsxs('div', { className: 'hermes-fb-preview', children: [
    jsxs('div', { className: 'hermes-fb-opsbar', children: [
      jsx('span', { style: { fontSize: 12, fontWeight: 600 }, children: entry.name }),
      jsx('span', { style: { flex: 1 } }),
      jsx('button', { className: 'hermes-fb-tbtn', style: { width: 20, height: 20, fontSize: 12 }, title: t('closePreview'), onClick: () => select(null), children: '×' }),
    ] }),
    jsxs('div', { className: 'hermes-fb-kv', children: [jsx('span', { children: t('origPath') }), jsx('b', { children: entry.orig || '—' })] }),
    jsxs('div', { className: 'hermes-fb-kv', children: [jsx('span', { children: t('deletedOn') }), jsx('b', { children: entry.deleted ? fmtDate(entry.deleted) : '—' })] }),
    jsxs('div', { className: 'hermes-fb-kv', children: [jsx('span', { children: 'Size' }), jsx('b', { children: entry.is_dir ? '—' : fmtSize(entry.size) })] }),
    jsxs('div', { className: 'hermes-fb-actions', children: [
      jsx(Button, { size: 'micro', onClick: doRestore, disabled: busy, children: t('restore') }),
    ] }),
  ] })
}

function Lightbox({ ctx, t, images, index, onNav, onClose }) {
  const entry = images[index]
  const [zoom, setZoom] = useState(1)
  useEffect(() => { setZoom(1) }, [entry && entry.path])
  const big = useQuery({
    queryKey: [ID, '/thumb', entry && entry.path, 1200, entry && entry.mtime],
    queryFn: ({ signal }) => ctx.rest('/thumb', { method: 'POST', timeoutMs: 60000, signal, body: { path: entry.path, size: 1200 } }),
    enabled: !!entry,
    staleTime: 300000,
    retry: false,
  })
  const info = useQuery({
    queryKey: [ID, '/imginfo', entry && entry.path, entry && entry.mtime],
    queryFn: ({ signal }) => ctx.rest('/imginfo', { method: 'POST', timeoutMs: 30000, signal, body: { path: entry.path } }),
    enabled: !!entry,
    staleTime: 300000,
    retry: false,
  })
  if (!entry) return null
  const meta = info.data?.ok
    ? `${info.data.width}×${info.data.height} · ${info.data.format}${info.data.exif && info.data.exif.camera ? ` · ${info.data.exif.camera}` : ''}${info.data.exif && info.data.exif.taken ? ` · ${info.data.exif.taken}` : ''}`
    : fmtSize(entry.size)
  const keys = ev => {
    if (ev.key === 'Escape') onClose()
    else if (ev.key === 'ArrowRight') onNav((index + 1) % images.length)
    else if (ev.key === 'ArrowLeft') onNav((index - 1 + images.length) % images.length)
    else if (ev.key === '+' || ev.key === '=') setZoom(z => Math.min(4, +(z + 0.25).toFixed(2)))
    else if (ev.key === '-') setZoom(z => Math.max(0.25, +(z - 0.25).toFixed(2)))
    else if (ev.key === '0') setZoom(1)
  }
  return jsxs('div', { className: 'hermes-fb-lb', tabIndex: 0, autoFocus: true, onKeyDown: keys, children: [
    jsxs('div', { className: 'hermes-fb-lbhead', children: [
      jsx('span', { style: { fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: entry.name }),
      jsx('span', { style: { color: 'var(--ui-text-tertiary)', whiteSpace: 'nowrap' }, children: meta }),
      jsx('span', { style: { flex: 1 } }),
      jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: `${index + 1} / ${images.length}` }),
      jsx('button', { className: 'hermes-fb-tbtn', title: t('zoomOut'), onClick: () => setZoom(z => Math.max(0.25, +(z - 0.25).toFixed(2))), children: '−' }),
      jsx('button', { className: 'hermes-fb-tbtn', title: t('zoomReset'), onClick: () => setZoom(1), children: `${Math.round(zoom * 100)}%` }),
      jsx('button', { className: 'hermes-fb-tbtn', title: t('zoomIn'), onClick: () => setZoom(z => Math.min(4, +(z + 0.25).toFixed(2))), children: '+' }),
      images.length > 1 && jsx('button', { className: 'hermes-fb-tbtn', onClick: () => onNav((index - 1 + images.length) % images.length), children: '‹' }),
      images.length > 1 && jsx('button', { className: 'hermes-fb-tbtn', onClick: () => onNav((index + 1) % images.length), children: '›' }),
      jsx('button', { className: 'hermes-fb-tbtn', title: t('closePreview'), onClick: onClose, children: '×' }),
    ] }),
    jsx('div', { className: 'hermes-fb-lbbody', children:
      big.isError
        ? jsx('div', { className: 'hermes-fb-error', children: t('mediaBlocked') })
        : !big.data?.data_url
          ? jsx(GlyphSpinner, { ariaLabel: entry.name })
          : jsx('img', { src: big.data.data_url, alt: entry.name, style: zoom === 1 ? { maxWidth: '100%', maxHeight: '100%' } : { width: `${zoom * 100}%`, maxWidth: 'none', cursor: 'zoom-out' }, onClick: () => { if (zoom !== 1) setZoom(1) } }),
    }),
  ] })
}

function Preview({ ctx, t, entry, refetch, select, onExpand }) {
  if (entry.path.startsWith('trash://')) return jsx(TrashPreview, { ctx, t, entry, refetch, select })
  const isImg = IMG_EXTS.has(entry.ext)
  const isAudio = AUD_EXTS.has(entry.ext)
  const isVideo = VID_EXTS.has(entry.ext)
  const isModel = MODEL_EXTS.has(entry.ext)
  const isTextish = !entry.is_dir && !isImg && !isAudio && !isVideo && !isModel && entry.ext !== '.pdf'
  const isPdf = !entry.is_dir && entry.ext === '.pdf'
  const [mediaErr, setMediaErr] = useState(false)
  const [dlFallback, setDlFallback] = useState(false)
  const mediaSrc = dlFallback ? fileDlUrl(entry.path) : fileUrl(entry.path)
  const onMediaError = () => { if (!dlFallback) setDlFallback(true); else setMediaErr(true) }
  const [fullRead, setFullRead] = useState(false)
  const [editMode, setEditMode] = useState(false)
  const [editText, setEditText] = useState('')
  const doSaveEdit = async () => {
    setOpBusy(true)
    try {
      const r = await ctx.rest('/write', { method: 'POST', timeoutMs: 30000, body: { path: entry.path, text: editText } })
      setOpBusy(false)
      if (!r?.ok) { opErr((r && r.error) || 'save failed'); return }
      setEditMode(false)
      refetch()
    } catch (e) { opErr(e); setOpBusy(false) }
  }
  const [renameMode, setRenameMode] = useState(false)
  const [renameName, setRenameName] = useState(entry.name)
  const [confirmDel, setConfirmDel] = useState(false)
  const [opBusy, setOpBusy] = useState(false)
  const clip = useValue(clipboard)
  const opErr = e => hostNotify(ctx, String((e && e.data && e.data.error) || (e && e.message) || e))
  if (pendingRename.get() === entry.path) { pendingRename.set(null); if (!renameMode) { setRenameName(entry.name); setConfirmDel(false); setRenameMode(true) } }
  if (pendingDelete.get() === entry.path) { pendingDelete.set(null); if (!confirmDel) setConfirmDel(true) }
  const doPasteHere = async () => {
    const c = clipboard.get()
    const dest = entry.path.replace(/[/\\][^/\\]+$/, '') || entry.path
    setOpBusy(true)
    try {
      if (c && c.paths?.length) {
        const r = await runJob(ctx, c.mode === 'cut' ? 'move' : 'copy', { paths: c.paths, dest_dir: dest }, c.mode === 'cut' ? t('jobMove', c.paths.length) : t('jobCopy', c.paths.length))
        if (!r?.ok) opErr((r?.error || r?.failed?.[0]?.error) || 'paste failed')
        else if (c.mode === 'cut') clipboard.set(null)
      } else {
        const r = await ctx.rest('/paste-os', { method: 'POST', timeoutMs: 120000, body: { dest } })
        if (!r?.ok) opErr(t('nothingToPaste'))
      }
      refetch()
    } catch (e) { opErr(e) }
    setOpBusy(false)
  }
  const doRename = async () => {
    const name = (renameName || '').trim()
    if (!name || name === entry.name) { setRenameMode(false); return }
    setOpBusy(true)
    try {
      const r = await ctx.rest('/rename', { method: 'POST', timeoutMs: 15000, body: { path: entry.path, new_name: name } })
      setRenameMode(false)
      refetch()
      if (r?.path) select(r.path)
    } catch (e) { opErr(e) }
    setOpBusy(false)
  }
  const doDelete = async () => {
    if (!confirmDel) { setConfirmDel(true); return }
    setOpBusy(true)
    try {
      const r = await ctx.rest('/delete', { method: 'POST', timeoutMs: 30000, body: { paths: [entry.path] } })
      if (!r?.ok) opErr((r?.failed?.[0]?.error) || 'delete failed')
      select(null)
      refetch()
    } catch (e) { opErr(e) }
    setConfirmDel(false)
    setOpBusy(false)
  }
  const doZip = async () => {
    setOpBusy(true)
    try {
      const r = await runJob(ctx, 'zip', { paths: [entry.path] }, t('jobZip', 1))
      if (r && r.error && !r.job_id) opErr(r.error)
      refetch()
    } catch (e) { opErr(e) }
    setOpBusy(false)
  }
  const doExtract = async () => {
    setOpBusy(true)
    try {
      const r = await runJob(ctx, 'extract', { path: entry.path }, t('jobExtract', entry.name))
      if (r?.path) hostNotify(ctx, 'Extracted to ' + r.path)
      else if (r && !r.ok) opErr(r.error || 'extract failed')
      refetch()
    } catch (e) { opErr(e) }
    setOpBusy(false)
  }
  const thumb = useQuery({
    queryKey: [ID, 'thumb', entry.path, entry.mtime, 'pv'],
    queryFn: ({ signal }) => ctx.rest('/thumb', { method: 'POST', timeoutMs: 15000, signal, body: { path: entry.path, size: 512 } }),
    enabled: isImg,
    staleTime: 300000,
    retry: false,
  })
  const text = useQuery({
    queryKey: [ID, 'read', entry.path, entry.mtime],
    queryFn: ({ signal }) => ctx.rest('/read', { method: 'POST', timeoutMs: 15000, signal, body: { path: entry.path, max_kb: 64 } }),
    enabled: isTextish && entry.size < 2 * 1024 * 1024,
    staleTime: 60000,
    retry: false,
  })
  const full = useQuery({
    queryKey: [ID, 'readfull', entry.path, entry.mtime],
    queryFn: ({ signal }) => ctx.rest('/read', { method: 'POST', timeoutMs: 20000, signal, body: { path: entry.path, max_kb: 1024 } }),
    enabled: fullRead && isTextish,
    staleTime: 60000,
    retry: false,
  })
  // NB: editBase must stay BELOW the text/full queries — it reads their data
  // (a use-before-declaration here crashes the whole pane on every click).
  const editBase = fullRead ? full.data : text.data
  const canEdit = isTextish && !entry.is_dir && entry.size < 256 * 1024 && !!editBase && !editBase.is_binary
  const startEdit = () => { setEditText(editBase.text || ''); setEditMode(true) }
  const openFile = async () => {
    const url = `file:///${entry.path.replace(/\\/g, '/')}`
    try {
      const ok = await ctx.os.openExternal(url)
      if (!ok) throw new Error('no handler')
    } catch {
      try { await ctx.os.revealPath(entry.path.replace(/[/\\][^/\\]+$/, '') || entry.path) } catch { /* noop */ }
      hostNotify(ctx, t('openFailed'))
    }
  }
  return jsxs('div', { className: 'hermes-fb-preview', children: [
    jsxs('div', { className: 'hermes-fb-opsbar', children: [
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => ctx.os.writeClipboard(entry.path), children: t('copyPath') }),
      renameMode && jsx('input', { value: renameName, onChange: e => setRenameName(e.target.value), onKeyDown: e => { if (e.key === 'Enter') doRename() }, autoFocus: true, 'aria-label': t('rename'), style: { flex: 1, minWidth: 60, height: 24, fontSize: 12, padding: '0 6px', borderRadius: 4, border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)' } }),
      renameMode && jsx(Button, { size: 'micro', onClick: doRename, disabled: opBusy, children: t('save') }),
      renameMode && jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => setRenameMode(false), children: t('cancel') }),
      !renameMode && jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => { setRenameName(entry.name); setConfirmDel(false); setRenameMode(true) }, children: t('rename') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => clipboard.set({ mode: 'copy', paths: [entry.path], label: entry.name }), children: t('copy') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => clipboard.set({ mode: 'cut', paths: [entry.path], label: entry.name }), children: t('cut') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: doPasteHere, disabled: opBusy, children: t('paste') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: doDelete, disabled: opBusy, children: confirmDel ? t('confirmDelete') : t('delete') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: doZip, disabled: opBusy, children: t('compress') }),
      entry.ext === '.zip' && jsx(Button, { size: 'micro', variant: 'ghost', onClick: doExtract, disabled: opBusy, children: t('extract') }),
      canEdit && !editMode && jsx(Button, { size: 'micro', variant: 'ghost', onClick: startEdit, children: t('editFile') }),
      jsx('span', { style: { flex: 1 } }),
      jsx('button', { className: 'hermes-fb-tbtn', style: { width: 20, height: 20, fontSize: 12 }, title: t('closePreview'), onClick: () => select(null), children: '×' }),
    ] }),
    jsxs('div', { className: 'hermes-fb-kv', children: [jsx('span', { children: t('details') }), jsx('b', { children: entry.name })] }),
    jsxs('div', { className: 'hermes-fb-kv', children: [jsx('span', { children: 'Size' }), jsx('b', { children: entry.is_dir ? '—' : fmtSize(entry.size) })] }),
    jsxs('div', { className: 'hermes-fb-kv', children: [jsx('span', { children: 'Modified' }), jsx('b', { children: fmtDate(entry.mtime) })] }),
    isImg && thumb.data?.data_url && jsx('img', { src: thumb.data.data_url, alt: entry.name, loading: 'lazy', style: onExpand ? { cursor: 'zoom-in' } : undefined, onClick: onExpand }),
    isAudio && !mediaErr && jsx('audio', { key: mediaSrc, controls: true, preload: 'metadata', src: mediaSrc, style: { width: '100%', marginTop: 4 }, onError: onMediaError }),
    isVideo && !mediaErr && jsx('video', { key: mediaSrc, controls: true, preload: 'metadata', src: mediaSrc, style: { width: '100%', maxHeight: 240, background: '#000', borderRadius: 6, marginTop: 4 }, onError: onMediaError }),
    (isAudio || isVideo) && mediaErr && jsx('div', { className: 'hermes-fb-error', children: t('mediaBlocked') }),
    isPdf && !mediaErr && jsx('iframe', { src: fileUrl(entry.path), title: entry.name, style: { width: '100%', height: 480, border: 0, borderRadius: 6, marginTop: 4, background: '#fff' }, onError: () => setMediaErr(true) }),
    isPdf && mediaErr && jsx('div', { className: 'hermes-fb-error', children: t('mediaBlocked') }),
    isModel && jsx(ModelView, { ctx, t, entry }),
    isTextish && text.data && !fullRead && !editMode && (text.data.is_binary
      ? jsx('div', { className: 'hermes-fb-error', children: t('binaryFile') })
      : jsxs('div', { children: [
        jsx('pre', { children: (text.data.text || '') + (text.data.truncated ? '…' : '') }),
        text.data.truncated && jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => setFullRead(true), children: t('readFull') }),
      ] })),
    isTextish && fullRead && !editMode && jsxs('div', { children: [
      full.isFetching && jsx(GlyphSpinner, { ariaLabel: t('readFull') }),
      full.data && !full.data.is_binary && jsx('pre', { children: (full.data.text || '') + (full.data.truncated ? '…' : '') }),
      full.data && full.data.is_binary && jsx('div', { className: 'hermes-fb-error', children: t('binaryFile') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => setFullRead(false), children: t('showLess') }),
    ] }),
    isTextish && editMode && jsxs('div', { children: [
      jsx('textarea', { value: editText, onChange: e => setEditText(e.target.value), spellCheck: false, style: { width: '100%', minHeight: 220, fontSize: 11, lineHeight: '15px', fontFamily: 'inherit', whiteSpace: 'pre', background: 'transparent', color: 'var(--ui-text-primary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6, padding: 6, marginTop: 4 } }),
      jsxs('div', { className: 'hermes-fb-actions', children: [
        jsx(Button, { size: 'micro', onClick: doSaveEdit, disabled: opBusy, children: t('save') }),
        jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => setEditMode(false), children: t('cancel') }),
      ] }),
    ] }),
    jsxs('div', { className: 'hermes-fb-actions', children: [
      !entry.is_dir && jsx(Button, { size: 'micro', onClick: openFile, children: t('openFile') }),
      isImg && onExpand && jsx(Button, { size: 'micro', variant: 'ghost', onClick: onExpand, children: t('expand') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => ctx.os.revealPath(entry.is_dir ? entry.path : entry.path.replace(/[/\\][^/\\]+$/, '')), children: t('reveal') }),
    ] }),
  ] })
}

function hostNotify(ctx, message) {
  try {
    if (ctx.host && typeof ctx.host.notify === 'function') ctx.host.notify({ kind: 'info', message })
  } catch { /* noop */ }
}

function PropsPanel({ ctx, t, path, onClose }) {
  const [calc, setCalc] = useState(false)
  const q = useQuery({
    queryKey: [ID, 'stat', path, calc],
    queryFn: ({ signal }) => ctx.rest('/stat', { method: 'POST', timeoutMs: 120000, signal, body: { path, dir_size: calc } }),
    retry: false,
  })
  const d = q.data
  const ds = d?.dir_size
  const rows = d?.ok ? [
    [t('location'), d.path],
    [t('type'), d.is_dir ? t('kindFolder') : ((d.ext || 'file').replace(/^\./, '').toUpperCase() + ' file')],
    [t('size'), d.is_dir ? (ds ? `${fmtSize(ds.bytes)} · ${ds.files} files · ${ds.dirs} folders${ds.capped ? ' (partial)' : ''}` : null) : fmtSize(d.size)],
    [t('modified'), fmtDate(d.mtime)],
    [t('created'), fmtDate(d.ctime)],
    [t('hidden'), d.hidden ? t('yes') : t('no')],
  ] : null
  return jsxs('div', { className: 'hermes-fb-preview', tabIndex: 0, autoFocus: true, onKeyDown: ev => { if (ev.key === 'Escape') onClose() }, children: [
    jsxs('div', { className: 'hermes-fb-opsbar', children: [
      jsx('span', { style: { fontSize: 12, fontWeight: 600 }, children: `${t('properties')} — ${d?.name || ''}` }),
      jsx('span', { style: { flex: 1 } }),
      jsx('button', { className: 'hermes-fb-tbtn', style: { width: 20, height: 20, fontSize: 12 }, title: t('closePreview'), onClick: onClose, children: '×' }),
    ] }),
    q.isError && jsx('div', { className: 'hermes-fb-error', children: t('backendDown') }),
    !q.isError && !d && jsx(GlyphSpinner, { ariaLabel: t('checkingUpdate') }),
    rows && rows.map(([k, v], i) => jsx('div', { className: 'hermes-fb-kv', children: [jsx('span', { children: k }), jsx('b', { children: v == null ? (k === t('size') ? jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => setCalc(true), children: calc ? t('checkingUpdate') : t('calcSize') }) : '—') : String(v) })] }, i)),
    d?.is_dir && !ds && jsx('div', { style: { fontSize: 10, color: 'var(--ui-text-quaternary)' }, children: t('calcNote') }),
  ] })
}

function MultiPanel({ ctx, t, entries, cwd, clearSel, refresh }) {
  const [confirmDel, setConfirmDel] = useState(false)
  const [busy, setBusy] = useState(false)
  const paths = entries.map(x => x.path)
  const totalSize = entries.reduce((a, x) => a + (x.is_dir ? 0 : (x.size || 0)), 0)
  const run = async fn => {
    setBusy(true)
    try { await fn(); clearSel(); refresh() } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
    setBusy(false)
    setConfirmDel(false)
  }
  const doCopy = () => { clipboard.set({ mode: 'copy', paths, label: `${entries.length} items` }) }
  const doCut = () => { clipboard.set({ mode: 'cut', paths, label: `${entries.length} items` }) }
  const doZip = () => run(async () => {
    const r = await runJob(ctx, 'zip', { paths }, t('jobZip', paths.length))
    if (!r?.ok) throw new Error(r?.error || (r?.failed?.[0]?.error) || 'compress failed')
  })
  const doDelete = () => {
    if (!confirmDel) { setConfirmDel(true); return }
    run(async () => {
      const r = await ctx.rest('/delete', { method: 'POST', timeoutMs: 120000, body: { paths } })
      if (!r?.ok) throw new Error((r?.failed?.[0]?.error) || 'delete failed')
    })
  }
  return jsxs('div', { className: 'hermes-fb-preview', children: [
    jsxs('div', { className: 'hermes-fb-opsbar', children: [
      jsx('span', { style: { fontSize: 12, fontWeight: 600 }, children: t('selectedN', entries.length, fmtSize(totalSize)) }),
      jsx('span', { style: { flex: 1 } }),
      jsx('button', { className: 'hermes-fb-tbtn', style: { width: 20, height: 20, fontSize: 12 }, title: t('clear'), onClick: clearSel, children: '×' }),
    ] }),
    jsxs('div', { className: 'hermes-fb-actions', children: [
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: doCopy, disabled: busy, children: t('copy') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: doCut, disabled: busy, children: t('cut') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: doDelete, disabled: busy, children: confirmDel ? t('confirmDelete') : t('delete') }),
      jsx(Button, { size: 'micro', variant: 'ghost', onClick: doZip, disabled: busy, children: t('compress') }),
    ] }),
  ] })
}

const MIME_BY_EXT = { mp3: 'audio/mpeg', wav: 'audio/wav', flac: 'audio/flac', m4a: 'audio/mp4', ogg: 'audio/ogg', opus: 'audio/opus', mp4: 'video/mp4', mkv: 'video/x-matroska', avi: 'video/x-msvideo', mov: 'video/quicktime', webm: 'video/webm', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', bmp: 'image/bmp', webp: 'image/webp', pdf: 'application/pdf', zip: 'application/zip', txt: 'text/plain' }
function dlUrl(path, name) {
  const ext = String(name).split('.').pop().toLowerCase()
  const mime = MIME_BY_EXT[ext] || 'application/octet-stream'
  const base = (typeof window !== 'undefined' && window.location && window.location.origin) || ''
  return `${mime}:${name}:${base}/api/plugins/athena/files/dl?path=${encodeURIComponent(path)}`
}

function TabPane({ ctx, tabId, initialCwd, sort, setSort, tilePx, setTilePx, showHidden, setShowHidden, sideWidth, setSideWidth, sideCollapsed, setSideCollapsed, sideOrder, setSideOrder, previewH, setPreviewH, viewMode, setViewMode, hidden, onCwd }) {
  const t = usePluginI18n(ID)
  const [cwd, setCwd] = useState(initialCwd || null)
  const [back, setBack] = useState([])
  const [fwd, setFwd] = useState([])
  const [sel, setSel] = useState([])
  const anchorRef = useRef(null)
  const clearSel = () => { setSel([]); anchorRef.current = null }
  const [propPath, setPropPath] = useState(null)
  const [lbIdx, setLbIdx] = useState(null)
  const [addrEdit, setAddrEdit] = useState(false)
  const [addrVal, setAddrVal] = useState('')
  const goAddr = async () => {
    const p = addrVal.trim()
    setAddrEdit(false)
    if (!p || p === cwd) return
    try {
      const r = await ctx.rest('/stat', { method: 'POST', timeoutMs: 15000, body: { path: p } })
      if (!r?.ok) { hostNotify(ctx, t('pathNotFound')); return }
      if (!r.is_dir) {
        hostNotify(ctx, t('notFolder'))
        nav(parentDir(p))
        setSel([r.path || p])
        return
      }
      nav(r.path || p)
    } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
  }
  const [search, setSearch] = useState('')
  // Debounced deep search: 2+ chars searches filenames under cwd recursively.
  // The instant filter still applies immediately; deep results replace it on arrival.
  const [deepQ, setDeepQ] = useState('')
  useEffect(() => {
    const q = search.trim()
    if (q.length < 2 || cwd === 'trash://') { setDeepQ(''); return }
    const id = setTimeout(() => setDeepQ(q), 350)
    return () => clearTimeout(id)
  }, [search, cwd])
  const [pins, setPins] = useState(() => ctx.storage.get('pins', null))
  const [mkdirMode, setMkdirMode] = useState(false)
  const [mkdirName, setMkdirName] = useState('')
  const [dropPath, setDropPath] = useState(null)
  const bump = useValue(dirBump)

  const roots = useQuery({
    queryKey: [ID, 'roots'],
    queryFn: ({ signal }) => ctx.rest('/roots', { method: 'POST', timeoutMs: 15000, signal, body: {} }),
    staleTime: 600000,
    retry: false,
  })

  const home = roots.data?.home || null
  const pinsEffective = pins || (home ? [home] : [])

  const list = useQuery({
    queryKey: [ID, 'list', cwd, showHidden, bump],
    queryFn: async ({ signal }) => {
      if (cwd === 'trash://') {
        const r = await ctx.rest('/trash', { method: 'POST', timeoutMs: 20000, signal, body: {} })
        const items = Array.isArray(r?.items) ? r.items : []
        return { entries: items, total: items.length, parent: null }
      }
      return ctx.rest('/list', { method: 'POST', timeoutMs: 20000, signal, body: { path: cwd, show_hidden: showHidden } })
    },
    enabled: !!cwd,
    staleTime: 10000,
    retry: false,
  })
  const [grepMode, setGrepMode] = useState(false)
  const isTrash = cwd === 'trash://'
  const deepActive = !isTrash && deepQ.length >= 2 && !grepMode && !zipView
  const deep = useQuery({
    queryKey: [ID, 'search', cwd, deepQ, showHidden],
    queryFn: ({ signal }) => ctx.rest('/search', { method: 'POST', timeoutMs: 60000, signal, body: { root: cwd, query: deepQ, show_hidden: showHidden } }),
    enabled: !!cwd && deepActive,
    staleTime: 30000,
    retry: false,
  })
  const grepActive = grepMode && !isTrash && deepQ.length >= 2 && !zipView
  const grep = useQuery({
    queryKey: [ID, 'grep', cwd, deepQ, showHidden],
    queryFn: ({ signal }) => ctx.rest('/grep', { method: 'POST', timeoutMs: 120000, signal, body: { root: cwd, pattern: deepQ, show_hidden: showHidden } }),
    enabled: !!cwd && grepActive,
    staleTime: 30000,
    retry: false,
  })
  const grepShowing = grepActive && !!grep.data?.hits
  const [zipView, setZipView] = useState(null)
  const zipList = useQuery({
    queryKey: [ID, 'ziplist', zipView && zipView.zip],
    queryFn: ({ signal }) => ctx.rest('/ziplist', { method: 'POST', timeoutMs: 60000, signal, body: { path: zipView.zip } }),
    enabled: !!zipView,
    staleTime: 60000,
    retry: false,
  })
  const zEntries = useMemo(() => {
    if (!zipView || !zipList.data?.members) return []
    const prefix = zipView.inner ? zipView.inner + '/' : ''
    const seen = new Set()
    const out = []
    for (const m of zipList.data.members) {
      if (!m.name.startsWith(prefix)) continue
      const rest = m.name.slice(prefix.length)
      if (!rest) continue
      const slash = rest.indexOf('/')
      if (slash === -1) {
        out.push({ name: rest, path: `zipview://${zipView.zip}//${m.name}`, is_dir: false, size: m.size, mtime: m.mtime, ext: rest.includes('.') ? rest.slice(rest.lastIndexOf('.')).toLowerCase() : '', member: m.name })
      } else {
        const d = rest.slice(0, slash)
        if (!seen.has(d)) {
          seen.add(d)
          out.push({ name: d, path: `zipview://${zipView.zip}//${prefix}${d}`, is_dir: true, size: 0, mtime: 0, ext: '', member: prefix + d })
        }
      }
    }
    return out
  }, [zipView, zipList.data])
  const memberOpen = async e => {
    if (!zipView || e.is_dir) return
    try {
      const r = await ctx.rest('/zextract', { method: 'POST', timeoutMs: 60000, body: { path: zipView.zip, member: e.member } })
      if (!r?.ok) { hostNotify(ctx, (r && r.error) || 'extract failed'); return }
      const url = `file:///${String(r.path).replace(/\\/g, '/')}`
      try {
        const ok = await ctx.os.openExternal(url)
        if (!ok) throw new Error('no handler')
      } catch {
        try { await ctx.os.revealPath(String(r.path).replace(/[/\\][^/\\]+$/, '') || r.path) } catch { /* noop */ }
      }
    } catch (err) { hostNotify(ctx, String((err && err.message) || err)) }
  }
  const extractAllHere = async () => {
    if (!zipView || !cwd || isTrash) return
    try {
      const r = await runJob(ctx, 'extract', { path: zipView.zip, dest_dir: cwd }, t('jobExtract', zipBase))
      if (!r?.ok) hostNotify(ctx, (r && (r.error || (r.failed && r.failed[0] && r.failed[0].error))) || 'extract failed')
      else { list.refetch(); hostNotify(ctx, r.path) }
    } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
  }
  const job = useValue(activeJob)
  const jobSt = useValue(jobStatus)
  useEffect(() => {
    if (!job) return
    let dead = false
    const tick = async () => {
      try {
        const s = await ctx.rest('/jobstatus', { method: 'POST', timeoutMs: 15000, body: { job_id: job.id } })
        if (dead || !s || s.error) return
        jobStatus.set(s)
        if ((s.state === 'completed' || s.state === 'cancelled' || s.state === 'failed') && !jobDoneFlag) {
          jobDoneFlag = true
          list.refetch()
          dirBump.set(dirBump.get() + 1)
          if (jobResolve) { const r = jobResolve; jobResolve = null; r(s.result ?? s) }
        }
      } catch { /* gateway restarting — keep polling */ }
    }
    tick()
    const iv = setInterval(tick, 500)
    return () => { dead = true; clearInterval(iv) }
  }, [job])
  const [emptyConfirm, setEmptyConfirm] = useState(false)
  const doEmptyTrash = async () => {
    if (!emptyConfirm) { setEmptyConfirm(true); return }
    setEmptyConfirm(false)
    try {
      const r = await ctx.rest('/trash-empty', { method: 'POST', timeoutMs: 120000, body: {} })
      if (!r?.ok) hostNotify(ctx, 'empty failed')
      clearSel()
      list.refetch()
    } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
  }

  const nav = (path, push = true) => {
    if (!path || path === cwd) return
    if (push && cwd) {
      setBack(b => [...b.slice(-49), cwd])
      setFwd([])
    }
    setCwd(path)
    clearSel()
    setPropPath(null)
    setAddrEdit(false)
    setZipView(null)
    setSearch('')
  }

  const goBack = () => {
    if (zipView) {
      if (zipView.inner) setZipView({ zip: zipView.zip, inner: zipView.inner.split('/').slice(0, -1).join('/') })
      else setZipView(null)
      clearSel()
      return
    }
    if (!back.length) return
    const prev = back[back.length - 1]
    setBack(b => b.slice(0, -1))
    if (cwd) setFwd(f => [cwd, ...f].slice(0, 50))
    setCwd(prev)
    clearSel()
    setSearch('')
  }

  const goFwd = () => {
    if (!fwd.length) return
    const [next, ...rest] = fwd
    setFwd(rest)
    if (cwd) setBack(b => [...b.slice(-49), cwd])
    setCwd(next)
    clearSel()
    setSearch('')
  }

  const entries = useMemo(() => {
    // Deep results replace the folder filter once they arrive; until then
    // the instant filter covers the current folder with zero latency.
    const all = zipView ? zEntries : ((deepActive && deep.data?.entries) ? deep.data.entries : (list.data?.entries ?? []))
    const q = search.trim().toLowerCase()
    const filtered = (deepActive && deep.data?.entries) ? all : (q ? all.filter(e => e.name.toLowerCase().includes(q)) : all)
    const cmp = {
      name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }),
      size: (a, b) => (b.size - a.size) || a.name.localeCompare(b.name),
      mtime: (a, b) => (b.mtime - a.mtime) || a.name.localeCompare(b.name),
      type: (a, b) => (a.ext.localeCompare(b.ext)) || a.name.localeCompare(b.name),
    }[sort] || ((a, b) => 0)
    return [...filtered].sort((a, b) => ((b.is_dir ? 1 : 0) - (a.is_dir ? 1 : 0)) || cmp(a, b))
  }, [list.data, search, sort, deepActive, deep.data, zipView, zEntries])

  const deepShowing = deepActive && !!deep.data?.entries
  const total = deepShowing ? (deep.data.total ?? entries.length) : (list.data?.total ?? entries.length)
  const [renderCount, setRenderCount] = useState(RENDER_CAP)
  useEffect(() => { setRenderCount(RENDER_CAP) }, [cwd, search, sort, deepShowing, grepShowing, zipView])
  const shown = entries.slice(0, renderCount)
  const growOnScroll = e => {
    try {
      const el = e.currentTarget
      if (el.scrollTop + el.clientHeight > el.scrollHeight - 600 && renderCount < entries.length) {
        setRenderCount(Math.min(entries.length, renderCount + 400))
      }
    } catch { /* noop */ }
  }
  const zipBase = zipView ? String(zipView.zip).split(/[/\\]+/).filter(Boolean).pop() : ''
  const crumbs = zipView ? [zipBase, ...String(zipView.inner || '').split('/').filter(Boolean)] : (isTrash ? ['Recycle Bin'] : (cwd ? splitPath(cwd) : []))
  const zipGo = i => {
    if (!zipView) return
    setZipView({ zip: zipView.zip, inner: crumbs.slice(1, i + 1).join('/') })
    clearSel()
  }
  const selEntry = sel.length === 1 ? entries.find(e => e.path === sel[0]) : null
  const selEntries = sel.map(p => entries.find(e => e.path === p)).filter(Boolean)
  const images = entries.filter(e => IMG_EXTS.has(e.ext))
  const openLb = () => {
    if (!selEntry || !IMG_EXTS.has(selEntry.ext)) return
    const i = images.findIndex(x => x.path === selEntry.path)
    setLbIdx(i >= 0 ? i : 0)
  }
  const selectProp = v => { if (v) { setSel([v]) } else { clearSel() } }
  // Click gestures: plain = single, Ctrl/Cmd = toggle, Shift = range from anchor
  const clickSel = (entry, idx, ev) => {
    if (ev && ev.shiftKey && anchorRef.current != null && shown[anchorRef.current]) {
      const a = anchorRef.current
      const [lo, hi] = a < idx ? [a, idx] : [idx, a]
      setSel(shown.slice(lo, hi + 1).map(x => x.path))
      return
    }
    if (ev && (ev.ctrlKey || ev.metaKey)) {
      anchorRef.current = idx
      setSel(s => s.includes(entry.path) ? s.filter(p => p !== entry.path) : [...s, entry.path])
      return
    }
    anchorRef.current = idx
    setSel(s => (s.length === 1 && s[0] === entry.path) ? [] : [entry.path])
  }
  const pinned = cwd && pinsEffective.includes(cwd)
  // Sidebar: pins + recycle bin + drives in one draggable list. Order
  // persists (sideOrder); unknown ids (new pins/drives) append at the end.
  const sideItems = (() => {
    const items = [
      ...pinsEffective.map(p => ({ id: `pin:${p}`, kind: 'pin', path: p })),
      { id: 'trash:bin', kind: 'trash', path: 'trash://' },
      ...(roots.data?.drives ?? []).map(d => ({ id: `drive:${d.mount}`, kind: 'drive', path: d.mount, drive: d })),
    ]
    if (!Array.isArray(sideOrder)) return items
    const pos = new Map(sideOrder.map((id, i) => [id, i]))
    const rank = it => (pos.has(it.id) ? pos.get(it.id) : 1e9)
    return [...items].sort((a, b) => rank(a) - rank(b))
  })()
  const moveSideItem = (fromId, toId) => {
    if (!fromId || !toId || fromId === toId) return
    const ids = sideItems.map(x => x.id).filter(id => id !== fromId)
    const at = ids.indexOf(toId)
    ids.splice(at < 0 ? ids.length : at, 0, fromId)
    setSideOrder(ids)
  }
  const sideDragStart = id => ev => { try { ev.dataTransfer.setData('application/x-hermes-filebox-side', id) } catch {} try { ev.dataTransfer.effectAllowed = 'move' } catch {} }
  const sideDragOver = ev => { try { if (Array.from(ev.dataTransfer?.types || []).includes('application/x-hermes-filebox-side')) ev.preventDefault() } catch {} }
  const sideDrop = id => ev => { ev.preventDefault(); try { moveSideItem(ev.dataTransfer.getData('application/x-hermes-filebox-side'), id) } catch {} }

  const togglePin = () => {
    if (!cwd) return
    const next = pinned ? pinsEffective.filter(p => p !== cwd) : [...pinsEffective, cwd]
    setPins(next)
    ctx.storage.set('pins', next)
  }

  const doMkdir = async () => {
    const name = mkdirName.trim()
    if (!name || !cwd) return
    try {
      await ctx.rest('/mkdir', { method: 'POST', timeoutMs: 15000, body: { parent: cwd, name } })
      setMkdirName('')
      setMkdirMode(false)
      list.refetch()
    } catch { /* error surfaces on next list load */ }
  }

  const doMkfile = async () => {
    if (!cwd || isTrash) return
    try {
      const r = await ctx.rest('/mkfile', { method: 'POST', timeoutMs: 15000, body: { parent: cwd, name: 'New file.txt' } })
      if (!r?.ok) { hostNotify(ctx, (r && r.error) || 'create failed'); return }
      list.refetch()
      setSel([r.path])
      pendingRename.set(r.path)
    } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
  }

  // Auto-refresh: poll a cheap dir fingerprint; refetch only on change.
  const watchFp = useRef(null)
  useEffect(() => {
    watchFp.current = null
    if (!cwd || isTrash) return undefined
    const id = setInterval(async () => {
      try {
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
        const r = await ctx.rest('/watch', { method: 'POST', timeoutMs: 8000, body: { path: cwd } })
        if (!r?.fingerprint) return
        if (watchFp.current && watchFp.current !== r.fingerprint) list.refetch()
        watchFp.current = r.fingerprint
      } catch { /* offline moment — next tick retries */ }
    }, 4000)
    return () => clearInterval(id)
  }, [cwd, isTrash])

  const startDrag = e => {
    e.preventDefault()
    const x0 = e.clientX
    const w0 = sideWidth
    const move = ev => setSideWidth(Math.min(320, Math.max(80, Math.round(w0 + ev.clientX - x0))))
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const pasteTo = async dest => {
    if (typeof dest === 'string' && dest.startsWith('trash://')) { hostNotify(ctx, t('restoreFirst')); return }
    const c = clipboard.get()
    if (c && c.paths?.length) {
      try {
        const r = await runJob(ctx, c.mode === 'cut' ? 'move' : 'copy', { paths: c.paths, dest_dir: dest }, c.mode === 'cut' ? t('jobMove', c.paths.length) : t('jobCopy', c.paths.length))
        if (!r?.ok) hostNotify(ctx, ((r?.error || r?.failed?.[0]?.error) || 'paste failed'))
        else if (c.mode === 'cut') clipboard.set(null)
        list.refetch()
      } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
      return
    }
    // No FileBox clipboard — fall back to files copied in Explorer / desktop
    if (!dest) return
    try {
      const r = await ctx.rest('/paste-os', { method: 'POST', timeoutMs: 120000, body: { dest } })
      if (r?.ok) list.refetch()
      else hostNotify(ctx, t('nothingToPaste'))
    } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
  }

  const dropMove = async (paths, dest) => {
    const final = (paths || []).filter(p => typeof p === 'string' && p && p !== dest && parentDir(p) !== dest && !isZipV(p))
    if (!final.length || !dest) return
    try {
      const r = await runJob(ctx, 'move', { paths: final, dest_dir: dest }, t('jobMove', final.length))
      if (!r?.ok) hostNotify(ctx, ((r?.error || r?.failed?.[0]?.error) || 'move failed'))
      dirBump.set(dirBump.get() + 1)
    } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
  }

  const isZipV = p => typeof p === 'string' && p.startsWith('zipview://')
  const isVirt = p => typeof p === 'string' && p.startsWith('trash://')
  const quickOp = async (kind, target) => {
    try {
      if (kind === 'restore') {
        const paths = (sel.includes(target.path) && sel.length > 1 ? sel : [target.path]).filter(isVirt)
        if (!paths.length) return
        const r = await (async () => {
          const failed = []
          for (const p of paths) {
            try {
              const rr = await ctx.rest('/trash-restore', { method: 'POST', timeoutMs: 60000, body: { id: p } })
              if (!rr?.ok) failed.push(p)
            } catch { failed.push(p) }
          }
          return { ok: failed.length === 0, failed }
        })()
        if (!r.ok) hostNotify(ctx, 'restore failed')
        clearSel()
        list.refetch()
        return
      }
      if (isVirt(target.path)) { hostNotify(ctx, t('restoreFirst')); return }
      if (isZipV(target.path) && kind !== 'open') { hostNotify(ctx, t('zipOpBlocked')); return }
      // Non-destructive ops act on the whole selection when the target is in it
      const multi = sel.includes(target.path) && sel.length > 1
      const paths = multi ? selEntries.map(x => x.path) : [target.path]
      const label = multi ? `${selEntries.length} items` : target.name
      if (kind === 'open') { openEntry(target); return }
      if (kind === 'rename') { pendingRename.set(target.path); setSel([target.path]); return }
      if (kind === 'delete') { pendingDelete.set(target.path); setSel([target.path]); return }
      if (kind === 'copy') { clipboard.set({ mode: 'copy', paths, label }); return }
      if (kind === 'cut') { clipboard.set({ mode: 'cut', paths, label }); return }
      if (kind === 'copyout') {
        const ps = (multi ? paths : [target.path]).filter(p => !isVirt(p) && typeof p === 'string')
        if (!ps.length) return
        await ctx.rest('/copy-out', { method: 'POST', timeoutMs: 30000, body: { paths: ps } })
        return
      }
      if (kind === 'paste') { await pasteTo(target.is_dir ? target.path : cwd); return }
      if (kind === 'duplicate') {
        // Copy each entry into its own parent; backend auto-numbers.
        const byParent = new Map()
        for (const p of paths.filter(p => !isVirt(p))) {
          const parent = parentDir(p)
          if (!byParent.has(parent)) byParent.set(parent, [])
          byParent.get(parent).push(p)
        }
        for (const [parent, ps] of byParent) await runJob(ctx, 'copy', { paths: ps, dest_dir: parent }, t('jobCopy', ps.length))
        list.refetch()
        return
      }
      if (kind === 'copypath') { await ctx.os.writeClipboard(multi ? paths.join('\n') : target.path); return }
      if (kind === 'copyother' || kind === 'moveother') {
        const dest = otherPaneCwd(tabId, cwd)
        if (!dest || isTrash) { hostNotify(ctx, t('noOtherPane')); return }
        const r = await runJob(ctx, kind === 'moveother' ? 'move' : 'copy', { paths, dest_dir: dest }, kind === 'moveother' ? t('jobMove', paths.length) : t('jobCopy', paths.length))
        if (r && !r.ok) throw new Error(r.error || (r.failed && r.failed[0] && r.failed[0].error) || 'copy failed')
        list.refetch()
        return
      }
      if (kind === 'reveal') { await ctx.os.revealPath(target.is_dir ? target.path : target.path.replace(/[/\\][^/\\]+$/, '')); return }
      if (kind === 'compress') {
        const r = await runJob(ctx, 'zip', { paths }, t('jobZip', paths.length))
        if (r && !r.ok) throw new Error(r.error || (r.failed && r.failed[0] && r.failed[0].error) || 'compress failed')
      } else if (kind === 'extract') {
        const r = await runJob(ctx, 'extract', { path: target.path }, t('jobExtract', target.name))
        if (r && !r.ok) throw new Error(r.error || 'extract failed')
      }
      list.refetch()
    } catch (e) { hostNotify(ctx, String((e && e.message) || e)) }
  }

  const startPreviewDrag = e => {
    e.preventDefault()
    const grip = e.currentTarget
    const y0 = e.clientY
    const h0 = previewH
    try { grip.setPointerCapture(e.pointerId) } catch {}
    const move = ev => setPreviewH(Math.min(640, Math.max(140, Math.round(h0 + (y0 - ev.clientY)))))
    const up = () => {
      grip.removeEventListener('pointermove', move)
      grip.removeEventListener('pointerup', up)
      grip.removeEventListener('pointercancel', up)
    }
    grip.addEventListener('pointermove', move)
    grip.addEventListener('pointerup', up)
    grip.addEventListener('pointercancel', up)
  }

  const openEntry = async e => {
    if (e.path.startsWith('zipview://')) {
      if (e.is_dir) { setZipView({ zip: zipView.zip, inner: e.member }); clearSel() }
      else memberOpen(e)
      return
    }
    if (e.is_dir) { nav(e.path); return }
    const url = `file:///${e.path.replace(/\\/g, '/')}`
    try {
      const ok = await ctx.os.openExternal(url)
      if (!ok) throw new Error('no handler')
    } catch {
      try { await ctx.os.revealPath(e.path.replace(/[/\\][^/\\]+$/, '') || e.path) } catch { /* noop */ }
    }
  }

  // Shared row gestures for grid tiles AND list rows
  const rowEvents = (e, idx) => ({
    onClick: ev => clickSel(e, idx, ev),
    onContextMenu: () => { if (!sel.includes(e.path)) { anchorRef.current = idx; setSel([e.path]) } },
    onDragStart: isTrash ? undefined : (ev => {
      const paths = (sel.includes(e.path) && sel.length) ? sel : [e.path]
      try { ev.dataTransfer.setData('application/x-hermes-filebox-paths', JSON.stringify(paths)) } catch {}
      try {
        if (paths.length === 1) {
          const one = entries.find(x => x.path === paths[0])
          if (one && !one.is_dir) ev.dataTransfer.setData('DownloadURL', dlUrl(one.path, one.name))
        }
      } catch {}
      ev.dataTransfer.effectAllowed = 'copyMove'
      if (!sel.includes(e.path)) { anchorRef.current = idx; setSel([e.path]) }
    }),
    onDragOver: (e.is_dir && !isTrash) ? (ev => { if (hasFbPaths(ev)) { ev.preventDefault(); ev.dataTransfer.dropEffect = 'move'; setDropPath(e.path) } }) : undefined,
    onDragLeave: (e.is_dir && !isTrash) ? (ev => { if (!ev.currentTarget.contains(ev.relatedTarget)) setDropPath(cur => cur === e.path ? null : cur) }) : undefined,
    onDrop: (e.is_dir && !isTrash) ? (ev => { ev.preventDefault(); setDropPath(null); try { dropMove(JSON.parse(ev.dataTransfer.getData('application/x-hermes-filebox-paths') || '[]'), e.path) } catch {} }) : undefined,
    onDoubleClick: isTrash ? undefined : (() => openEntry(e)),
    onKeyDown: ev => { if (ev.key === 'Enter' && !isTrash) openEntry(e) },
  })
  const gridKeys = ev => {
    const tag = ev.target?.tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
    if (ev.key === 'Escape') { if (zipView) setZipView(null); clearSel(); return }
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'a' || ev.key === 'A')) { ev.preventDefault(); anchorRef.current = null; setSel(entries.map(x => x.path)) }
    else if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(ev.key) && entries.length) {
      ev.preventDefault()
      const dir = (ev.key === 'ArrowDown' || ev.key === 'ArrowRight') ? 1 : -1
      const cur = anchorRef.current ?? entries.findIndex(e => e.path === sel[sel.length - 1])
      const next = Math.max(0, Math.min(entries.length - 1, (cur < 0 ? (dir > 0 ? -1 : 0) : cur) + dir))
      if (next >= renderCount && renderCount < entries.length) setRenderCount(Math.min(entries.length, next + 100))
      if (ev.shiftKey && anchorRef.current != null && entries[anchorRef.current]) {
        const [a, b] = [anchorRef.current, next].sort((x, y) => x - y)
        setSel(entries.slice(a, b + 1).map(e => e.path))
      } else {
        anchorRef.current = next
        setSel([entries[next].path])
      }
      setTimeout(() => {
        try {
          const el = document.querySelector(`[data-fbpath="${CSS.escape(entries[next].path)}"]`)
          if (el) el.scrollIntoView({ block: 'nearest' })
        } catch { /* noop */ }
      }, 60)
    } else if (ev.key === 'F2' && sel.length === 1) {
      const hit = shown.find(e => e.path === sel[0])
      if (hit) quickOp('rename', hit)
    } else if (ev.key === 'Delete' && sel.length === 1) {
      const hit = shown.find(e => e.path === sel[0])
      if (hit) quickOp('delete', hit)
    } else if (ev.key === 'Enter' && !isTrash && selEntry) {
      if (ev.altKey) { ev.preventDefault(); setPropPath(selEntry.path) }
      else openEntry(selEntry)
    } else if ((ev.ctrlKey || ev.metaKey) && !ev.shiftKey && !ev.altKey && ['c', 'C', 'x', 'X', 'v', 'V'].includes(ev.key)) {
      if (ev.key === 'v' || ev.key === 'V') {
        if (!isTrash && cwd) quickOp('paste', { is_dir: true, path: cwd, name: '' })
      } else {
        // Let a real text selection (preview <pre>, rename box) copy normally.
        try { if (window.getSelection && String(window.getSelection())) return } catch {}
        if (!sel.length || isTrash || sel.some(isZipV)) return
        clipboard.set({ mode: ev.key.toLowerCase() === 'x' ? 'cut' : 'copy', paths: [...sel], label: sel.length === 1 ? (selEntries[0]?.name || sel[0]) : `${sel.length} items` })
      }
    } else if (ev.altKey && ev.key === 'ArrowUp') {
      ev.preventDefault()
      if (!isTrash && list.data?.parent) nav(list.data.parent)
    }
  }
  const menuItems = e => (isVirt(e.path) ? [
    jsx(ContextMenuItem, { onSelect: () => quickOp('restore', e), children: t('restore') }),
  ] : [
    jsx(ContextMenuItem, { onSelect: () => quickOp('open', e), children: t('openFile') }),
    jsx(ContextMenuItem, { onSelect: () => quickOp('rename', e), children: t('rename') }),
    jsx(ContextMenuSeparator, {}),
    jsx(ContextMenuItem, { onSelect: () => quickOp('copy', e), children: t('copy') }),
    jsx(ContextMenuItem, { onSelect: () => quickOp('cut', e), children: t('cut') }),
    jsx(ContextMenuItem, { onSelect: () => quickOp('copyout', e), children: t('copyOutside') }),
    jsx(ContextMenuItem, { onSelect: () => quickOp('paste', e), children: t('paste') }),
    jsx(ContextMenuItem, { onSelect: () => quickOp('duplicate', e), children: t('duplicate') }),
    jsx(ContextMenuSeparator, {}),
    jsx(ContextMenuItem, { onSelect: () => quickOp('compress', e), children: t('compress') }),
    e.ext === '.zip' && jsx(ContextMenuItem, { onSelect: () => quickOp('extract', e), children: t('extract') }),
    jsx(ContextMenuItem, { onSelect: () => quickOp('delete', e), variant: 'destructive', children: t('delete') }),
    jsx(ContextMenuSeparator, {}),
    jsx(ContextMenuItem, { onSelect: () => quickOp('copypath', e), children: t('copyPath') }),
    jsx(ContextMenuItem, { onSelect: () => quickOp('reveal', e), children: t('reveal') }),
    !e.is_dir && !isZipV(e.path) && jsx(ContextMenuItem, { onSelect: () => { try { window.__athenaSendToApp && window.__athenaSendToApp(e.path) } catch { /* nothing on screen to receive it */ } }, children: 'Send to an app\u2026' }),
    !isZipV(e.path) && jsx(ContextMenuItem, { onSelect: () => setPropPath(e.path), children: t('properties') }),
    isZipV(e.path) && jsx(ContextMenuItem, { onSelect: () => quickOp('open', e), children: e.is_dir ? t('openFile') : t('openMember') }),
    !isZipV(e.path) && !e.is_trash && otherPaneCwd(tabId, cwd) && jsx(ContextMenuItem, { onSelect: () => quickOp('copyother', e), children: `${t('copyToOther')} → ${String(otherPaneCwd(tabId, cwd)).split(/[/\\]+/).filter(Boolean).pop()}` }),
    !isZipV(e.path) && !e.is_trash && otherPaneCwd(tabId, cwd) && jsx(ContextMenuItem, { onSelect: () => quickOp('moveother', e), children: `${t('moveToOther')} → ${String(otherPaneCwd(tabId, cwd)).split(/[/\\]+/).filter(Boolean).pop()}` }),
    !e.path.startsWith('trash://') && !zipView && e.ext === '.zip' && jsx(ContextMenuItem, { onSelect: () => { setZipView({ zip: e.path, inner: '' }); clearSel() }, children: t('browseZip') }),
  ])

  if (!cwd && home) setCwd(home)
  useEffect(() => {
    paneCwds[tabId] = { cwd, visible: !hidden }
    if (onCwd && cwd) { try { onCwd(cwd) } catch { /* noop */ } }
    return () => { try { delete paneCwds[tabId] } catch { /* noop */ } }
  }, [tabId, cwd, hidden])

  return jsxs('div', { className: 'hermes-fb', style: hidden ? { display: 'none' } : { flex: 1, minHeight: 0, height: 'auto' }, children: [
    sideCollapsed && jsx('div', { className: 'hermes-fb-rail', children:
      jsx('button', { className: 'hermes-fb-tbtn', title: 'Show sidebar', onClick: () => setSideCollapsed(false), children: '»' }),
    }),
    !sideCollapsed && jsxs('div', { className: 'hermes-fb-nav', style: { width: sideWidth }, children: [
      jsxs('div', { style: { display: 'flex', alignItems: 'center' }, children: [
        jsx('div', { className: 'hermes-fb-group', style: { flex: 1 }, children: t('quickAccess') }),
        jsx('button', { className: 'hermes-fb-tbtn', style: { width: 20, height: 20, fontSize: 11 }, title: 'Hide sidebar', onClick: () => setSideCollapsed(true), children: '«' }),
      ] }),
      ...sideItems.map(it => {
        const label = it.kind === 'pin'
          ? (it.path.split(/[/\\]+/).filter(Boolean).pop() || it.path)
          : it.kind === 'trash' ? t('recycleBin') : `${it.drive.device} · ${it.drive.total_gb} GB`
        const icon = it.kind === 'pin' ? 'folder' : it.kind === 'trash' ? 'trash' : 'device-hard-drive'
        const btn = jsx('button', {
          className: 'hermes-fb-navbtn', 'data-on': it.path === cwd,
          draggable: true, onDragStart: sideDragStart(it.id), onDragOver: sideDragOver, onDrop: sideDrop(it.id),
          onClick: () => nav(it.path),
          children: [jsx(Codicon, { name: icon }), jsx('span', { children: label })],
        }, it.id)
        return it.kind === 'drive'
          ? jsxs('div', { onDragOver: sideDragOver, onDrop: sideDrop(it.id), children: [
              btn,
              jsx('div', { className: 'hermes-fb-drivebar', children: jsx('div', { style: { width: `${it.drive.percent}%` } }) }),
            ] }, it.id)
          : btn
      }),
    ] }),
    !sideCollapsed && jsx('div', { className: 'hermes-fb-grip', onMouseDown: startDrag }),
    jsxs('div', { className: 'hermes-fb-main', children: [
      jsxs('div', { className: 'hermes-fb-toolbar', children: [
        jsx('button', { className: 'hermes-fb-tbtn', disabled: !back.length, 'aria-label': t('back'), title: t('back'), onClick: goBack, children: '←' }),
        jsx('button', { className: 'hermes-fb-tbtn', disabled: !fwd.length, 'aria-label': t('fwd'), title: t('fwd'), onClick: goFwd, children: '→' }),
        jsx('button', { className: 'hermes-fb-tbtn', disabled: !list.data?.parent, 'aria-label': t('up'), title: t('up'), onClick: () => { if (zipView) goBack(); else if (list.data?.parent) nav(list.data.parent) }, children: '↑' }),
        jsx('button', { className: 'hermes-fb-tbtn', 'aria-label': t('refresh'), title: t('refresh'), onClick: () => list.refetch(), children: '↻' }),
        zipView && jsx('button', { className: 'hermes-fb-tbtn', style: { width: 'auto', fontSize: 11 }, title: t('closeZip'), onClick: () => { setZipView(null); clearSel() }, children: `📦 ${zipBase} ✕` }),
        jsx('div', { className: 'hermes-fb-crumbs', children:
          (addrEdit && !isTrash && !zipView)
            ? jsx('input', { className: 'hermes-fb-addr', value: addrVal, autoFocus: true,
                onFocus: e => { try { e.target.select() } catch {} },
                onChange: e => setAddrVal(e.target.value),
                onKeyDown: e => { if (e.key === 'Enter') goAddr(); else if (e.key === 'Escape') setAddrEdit(false); e.stopPropagation() },
                onBlur: () => setAddrEdit(false) })
            : crumbs.map((c, i) => {
            const upto = crumbs.slice(0, i + 1).join('\\') + (i === 0 && /^[A-Za-z]:\\$/.test(c) ? '' : '')
            const full = i === 0 ? c : `${crumbs[0]}${crumbs.slice(1, i + 1).join('\\')}`
            return jsxs('span', { style: { display: 'inline-flex', alignItems: 'center', flexShrink: 0 }, children: [
              i > 0 && jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: '›' }),
              (isTrash || i === crumbs.length - 1)
                ? (isTrash || zipView || addrEdit
                  ? jsx('span', { className: 'hermes-fb-crumb', 'data-on': true, children: c.replace(/\\$/, '') })
                  : jsx('button', { className: 'hermes-fb-crumb', 'data-on': true, title: t('editPath'), onClick: () => { setAddrVal(cwd); setAddrEdit(true) }, children: c.replace(/\\$/, '') }))
                : jsx('button', { className: 'hermes-fb-crumb', 'data-on': false, onClick: () => { if (zipView) zipGo(i); else nav(full) }, children: c.replace(/\\$/, '') }),
            ] }, i)
          }),
        }),
        !isTrash && jsx('button', { className: 'hermes-fb-tbtn', title: pinned ? t('unpinFolder') : t('pinFolder'), onClick: () => togglePin(cwd), children: pinned ? '★' : '☆' }),
        isTrash && jsx(Button, { size: 'micro', variant: 'ghost', title: t('emptyBinTip'), onClick: doEmptyTrash, children: emptyConfirm ? t('confirmEmpty') : t('emptyBin') }),
      ] }),
      jsxs('div', { className: 'hermes-fb-searchrow', children: [
        jsx('div', { className: 'hermes-fb-search', children: jsx(SearchField, {
          value: search, onChange: setSearch, placeholder: grepMode ? t('searchContent') : t('searchFiles'),
          containerClassName: '',
        }) }),
        jsx('button', { className: 'hermes-fb-tbtn', style: { width: 'auto', fontSize: 11, color: grepMode ? 'var(--ui-accent)' : undefined }, title: t('searchContent'), onClick: () => setGrepMode(!grepMode), children: grepMode ? '✓ content' : 'content' }),
        jsx('select', { className: 'hermes-fb-sort', value: sort, 'aria-label': t('sortBy'), onChange: e => setSort(e.target.value), children: [
          jsx('option', { value: 'name', children: t('sortName') }),
          jsx('option', { value: 'size', children: t('sortSize') }),
          jsx('option', { value: 'mtime', children: t('sortDate') }),
          jsx('option', { value: 'type', children: t('sortType') }),
        ] }),
        viewMode === 'grid' && jsx('select', { className: 'hermes-fb-sort', value: tilePx, 'aria-label': 'tile size', onChange: e => setTilePx(Number(e.target.value)), children: [
          jsx('option', { value: 64, children: 'S' }),
          jsx('option', { value: 88, children: 'M' }),
          jsx('option', { value: 112, children: 'L' }),
          jsx('option', { value: 144, children: 'XL' }),
        ] }),
        jsx('button', { className: 'hermes-fb-tbtn', style: { width: 26, height: 22, fontSize: 13 }, title: viewMode === 'grid' ? t('viewList') : t('viewGrid'), onClick: () => setViewMode(viewMode === 'grid' ? 'list' : 'grid'), children: viewMode === 'grid' ? '☰' : '▦' }),
      ] }),
      mkdirMode && !isTrash && jsxs('div', { className: 'hermes-fb-mkdir', children: [
        jsx('input', { value: mkdirName, placeholder: t('newFolder'), onChange: e => setMkdirName(e.target.value), onKeyDown: e => { if (e.key === 'Enter') doMkdir() } }),
        jsx(Button, { size: 'micro', onClick: doMkdir, children: t('create') }),
        jsx(Button, { size: 'micro', variant: 'ghost', onClick: () => { setMkdirMode(false); setMkdirName('') }, children: t('cancel') }),
      ] }),
      grepShowing ? jsxs('div', { className: 'hermes-fb-list', tabIndex: 0, children: [
        jsx('div', { className: 'hermes-fb-status', children: [
          t('grepHits', grep.data.total, new Set(grep.data.hits.map(h => h.path)).size),
          grep.data.truncated ? ` · ${t('grepMore')}` : null,
        ].filter(Boolean).join('') }),
        grep.data.hits.length === 0 && !grep.isFetching && jsx('div', { className: 'hermes-fb-status', children: t('noGrepHits') }),
        ...grep.data.hits.map(h => jsx('button', {
          className: 'hermes-fb-lrow', style: { display: 'block', width: '100%', textAlign: 'left', background: 'none', border: 0, cursor: 'pointer' },
          onClick: () => { nav(parentDir(h.path)); setSel([h.path]) },
          children: [
            jsxs('div', { style: { fontSize: 12 }, children: [jsx('b', { children: h.name }), jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: `  ·  L${h.line}` })] }),
            jsx('div', { style: { fontSize: 11, color: 'var(--ui-text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: h.text }),
          ],
        }, `${h.path}:${h.line}`)),
      ] }) : (viewMode === 'list' ? jsxs('div', { className: 'hermes-fb-list', tabIndex: 0, onKeyDown: gridKeys, onScroll: growOnScroll, children: [
        jsxs('div', { className: 'hermes-fb-lhead', children: [
          jsx('button', { className: 'hermes-fb-lhbtn', 'data-on': sort === 'name', onClick: () => setSort('name'), children: t('cName') }),
          jsx('button', { className: 'hermes-fb-lhbtn', 'data-on': sort === 'size', onClick: () => setSort('size'), children: t('cSize') }),
          jsx('button', { className: 'hermes-fb-lhbtn', 'data-on': sort === 'mtime', onClick: () => setSort('mtime'), children: t('cModified') }),
          jsx('button', { className: 'hermes-fb-lhbtn', 'data-on': sort === 'type', onClick: () => setSort('type'), children: t('cType') }),
        ] }),
        list.isFetching && !list.data && jsx(GlyphSpinner, { ariaLabel: t('explorer') }),
        list.isError && jsx('div', { className: 'hermes-fb-error', role: 'status', children: t('backendDown') }),
        ...shown.map((e, idx) => jsx(ContextMenu, { children: [
          jsx(ContextMenuTrigger, { asChild: true, children: jsxs('div', {
            className: 'hermes-fb-lrow', 'data-on': sel.includes(e.path), 'data-dir': e.is_dir, 'data-fbpath': e.path,
            'data-drop': dropPath === e.path, role: 'button', tabIndex: 0,
            draggable: !isTrash,
            ...rowEvents(e, idx),
            'aria-label': e.name,
            children: [
              jsxs('span', { className: 'hermes-fb-lname', children: [jsx(Codicon, { name: e.is_dir ? 'folder' : iconFor(category(e)), size: 14 }), jsx('span', { children: e.name })] }),
              jsx('span', { className: 'hermes-fb-lnum', children: e.is_dir ? '—' : fmtSize(e.size) }),
              jsx('span', { children: fmtDate(e.mtime) }),
              jsx('span', { children: e.is_dir ? t('folder') : (e.ext ? e.ext.replace(/^\./, '').toUpperCase() : '—') }),
            ],
          }) }),
          jsx(ContextMenuContent, { children: menuItems(e) }),
        ] }, e.path)),
      ] }) : jsxs('div', { className: 'hermes-fb-grid', tabIndex: 0, style: { gridTemplateColumns: `repeat(auto-fill,minmax(${tilePx + 16}px,1fr))` }, onDragOver: ev => { if (hasFbPaths(ev)) ev.preventDefault() }, onKeyDown: gridKeys, onScroll: growOnScroll, onDrop: ev => {
        if (isTrash) return
        if (ev.target?.closest && ev.target.closest('[data-dir="true"]')) return
        ev.preventDefault()
        try { dropMove(JSON.parse(ev.dataTransfer.getData('application/x-hermes-filebox-paths') || '[]'), cwd) } catch {}
      }, children: [
        list.isFetching && !list.data && jsx(GlyphSpinner, { ariaLabel: t('explorer') }),
        list.isError && jsx('div', { className: 'hermes-fb-error', role: 'status', children: t('backendDown') }),
        ...shown.map((e, idx) => {
          const cat = category(e)
          const iconPx = Math.max(24, Math.round(tilePx * 0.42))
          return jsx(ContextMenu, { children: [
          jsx(ContextMenuTrigger, { asChild: true, children: jsx('button', {
            className: 'hermes-fb-tile', 'data-on': sel.includes(e.path), 'data-dir': e.is_dir, 'data-fbpath': e.path,
            'data-drop': dropPath === e.path,
            draggable: !isTrash,
            ...rowEvents(e, idx),
            'aria-label': e.name,
            children: [
              jsx(Thumb, { ctx, entry: e, px: iconPx, fallback: jsx('span', { className: 'hermes-fb-tileicon', children: jsx(Codicon, { name: iconFor(cat), size: iconPx }) }) }),
              jsx('span', { className: 'hermes-fb-tilename', children: e.name }),
              jsx('span', { className: 'hermes-fb-tilesub', children: e.is_dir ? '—' : fmtSize(e.size) }),
            ],
          }) }),
          jsx(ContextMenuContent, { children: menuItems(e) }),
        ] }, e.path)
        }),
      ] })),
      total > shown.length && jsx('div', { className: 'hermes-fb-status', children: t('showingFirst', shown.length, total) }),
      grepActive && grep.isFetching && !grepShowing && jsx('div', { className: 'hermes-fb-status', children: t('searchingSub') }),
      zipView && zipList.isError && jsx('div', { className: 'hermes-fb-error', role: 'status', children: t('backendDown') }),
      zipView && zipList.isFetching && !zipList.data && jsx(GlyphSpinner, { ariaLabel: zipBase }),
      job && jobSt?.state === 'running' && jsxs('div', { className: 'hermes-fb-status', children: [
        jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children:
          `${job.label} — ${jobSt.total_bytes > 0 ? Math.round(100 * jobSt.done_bytes / jobSt.total_bytes) : (jobSt.total_files ? Math.round(100 * jobSt.done_files / jobSt.total_files) : 0)}%${jobSt.current ? ` · ${jobSt.current}` : ''}` }),
        jsx('span', { style: { flex: 1 } }),
        jsx('button', { className: 'hermes-fb-tbtn', style: { width: 'auto', fontSize: 11 }, title: t('cancelJob'), onClick: () => cancelJob(ctx), children: `✕ ${t('cancelJob')}` }),
      ] }),
      job && jobSt && jobSt.state !== 'running' && jsxs('div', { className: 'hermes-fb-status', children: [
        jsx('span', { children: `${job.label}: ${jobSt.state}${jobSt.result?.failed?.length ? ` (${jobSt.result.failed.length} failed)` : ''}${jobSt.result?.error ? ` — ${jobSt.result.error}` : ''}` }),
        jsx('span', { style: { flex: 1 } }),
        jsx('button', { className: 'hermes-fb-tbtn', style: { width: 'auto', fontSize: 11 }, onClick: () => { activeJob.set(null); jobStatus.set(null) }, children: '✕' }),
      ] }),
      deepActive && deep.isFetching && !deepShowing && jsx('div', { className: 'hermes-fb-status', children: t('searchingSub') }),
      deepShowing && jsx('div', { className: 'hermes-fb-status', children: [
        t('searchDeep', deep.data.total ?? entries.length, tabName(cwd)),
        deep.data.truncated ? ` · ${t('searchMore')}` : null,
      ].filter(Boolean).join('') }),
      jsxs('div', { className: 'hermes-fb-status', children: [
        jsx('span', { children: t('items', total) }),
        selEntry && jsx('span', { children: `${selEntry.name} · ${selEntry.is_dir ? '' : `${fmtSize(selEntry.size)} · `}${fmtDate(selEntry.mtime)}` }),
        selEntries.length > 1 && jsx('span', { children: t('selectedN', selEntries.length, fmtSize(selEntries.reduce((a, x) => a + (x.is_dir ? 0 : (x.size || 0)), 0))) }),
        jsx('span', { style: { flex: 1 } }),
        !isTrash && jsx('button', { className: 'hermes-fb-tbtn', style: { width: 'auto', fontSize: 11 }, onClick: () => setMkdirMode(!mkdirMode), children: `+ ${t('newFolder')}` }),
        !isTrash && jsx('button', { className: 'hermes-fb-tbtn', style: { width: 'auto', fontSize: 11 }, onClick: doMkfile, children: `+ ${t('newFile')}` }),
        jsx('button', { className: 'hermes-fb-tbtn', style: { width: 'auto', fontSize: 11 }, onClick: () => setShowHidden(!showHidden), children: showHidden ? '✓ hidden' : 'hidden' }),
      ] }),
      propPath ? jsxs('div', { className: 'hermes-fb-pvwrap', style: { height: previewH }, children: [
        jsx('div', { className: 'hermes-fb-griph', title: t('dragResize'), onPointerDown: startPreviewDrag }),
        jsx(PropsPanel, { ctx, t, path: propPath, onClose: () => setPropPath(null) }, propPath),
      ] }) : selEntry && (isZipV(selEntry.path) ? jsxs('div', { className: 'hermes-fb-pvwrap', style: { height: previewH }, children: [
        jsx('div', { className: 'hermes-fb-griph', title: t('dragResize'), onPointerDown: startPreviewDrag }),
        jsxs('div', { className: 'hermes-fb-preview', children: [
          jsxs('div', { className: 'hermes-fb-opsbar', children: [
            jsx('span', { style: { fontSize: 12, fontWeight: 600 }, children: selEntry.name }),
            jsx('span', { style: { flex: 1 } }),
            jsx('button', { className: 'hermes-fb-tbtn', style: { width: 20, height: 20, fontSize: 12 }, title: t('closePreview'), onClick: () => selectProp(null), children: '×' }),
          ] }),
          jsxs('div', { className: 'hermes-fb-kv', children: [jsx('span', { children: t('size') }), jsx('b', { children: selEntry.is_dir ? '—' : fmtSize(selEntry.size) })] }),
          jsxs('div', { className: 'hermes-fb-actions', children: [
            !selEntry.is_dir && jsx(Button, { size: 'micro', onClick: () => memberOpen(selEntry), children: t('openMember') }),
            jsx(Button, { size: 'micro', variant: 'ghost', onClick: extractAllHere, children: t('extractAll') }),
          ] }),
        ] }),
      ] }) : (selEntry.is_dir ? null : jsxs('div', { className: 'hermes-fb-pvwrap', style: { height: previewH }, children: [
        // A folder selected by a single click draws nothing down here: the click is a selection,
        // and the panel covering the last rows of the list while you aim at a folder to open it
        // is the panel getting in the way of the gesture it interrupted. Double click walks in;
        // the status bar still names the folder, and the context menu still offers Properties.
        // Files keep the one-click preview, because for a file the click *is* the action.
        jsx('div', { className: 'hermes-fb-griph', title: t('dragResize'), onPointerDown: startPreviewDrag }),
        jsx(Preview, { ctx, t, entry: selEntry, refetch: () => list.refetch(), select: selectProp, onExpand: IMG_EXTS.has(selEntry.ext) ? openLb : undefined }, selEntry.path),
      ] }))),
      selEntries.length > 1 && jsx(MultiPanel, { ctx, t, entries: selEntries, cwd, clearSel, refresh: () => { list.refetch(); dirBump.set(dirBump.get() + 1) } }),
    ] }),
    lbIdx != null && images[lbIdx] && jsx(Lightbox, { ctx, t, images, index: lbIdx, onNav: setLbIdx, onClose: () => setLbIdx(null) }),
  ] })
}

function tabName(cwd) {
  if (!cwd) return 'Home'
  const parts = String(cwd).split(/[/\\]+/).filter(Boolean)
  return parts.pop() || cwd
}


function Explorer({ ctx }) {
  const t = usePluginI18n(ID)
  const roots = useQuery({
    queryKey: [ID, 'roots'],
    queryFn: ({ signal }) => ctx.rest('/roots', { method: 'POST', timeoutMs: 15000, signal, body: {} }),
    staleTime: 600000,
    retry: false,
  })
  const home = roots.data?.home || null
  const [sort, setSortState] = useState(() => ctx.storage.get('sort', 'name'))
  const setSort = v => { setSortState(v); try { ctx.storage.set('sort', v) } catch {} }
  const [tilePx, setTilePxState] = useState(() => Number(ctx.storage.get('tilePx', 88)) || 88)
  const setTilePx = v => { const n = Number(v) || 88; setTilePxState(n); try { ctx.storage.set('tilePx', n) } catch {} }
  const [showHidden, setShowHiddenState] = useState(() => ctx.storage.get('showHidden', false) === true)
  const setShowHidden = v => { setShowHiddenState(v); try { ctx.storage.set('showHidden', v) } catch {} }
  const [sideWidth, setSideWidthState] = useState(() => Number(ctx.storage.get('sideWidth', 148)) || 148)
  const setSideWidth = v => { const n = Number(v) || 148; setSideWidthState(n); try { ctx.storage.set('sideWidth', n) } catch {} }
  const [sideCollapsed, setSideCollapsedState] = useState(() => ctx.storage.get('sideCollapsed', false) === true)
  const setSideCollapsed = v => { setSideCollapsedState(v); try { ctx.storage.set('sideCollapsed', v) } catch {} }
  const [sideOrder, setSideOrderState] = useState(() => { try { const o = ctx.storage.get('sideOrder', null); return Array.isArray(o) ? o : null } catch { return null } })
  const setSideOrder = v => { setSideOrderState(v); try { ctx.storage.set('sideOrder', v) } catch {} }
  const [previewH, setPreviewHState] = useState(() => Number(ctx.storage.get('previewH', 280)) || 280)
  const setPreviewH = v => { const n = Number(v) || 280; setPreviewHState(n); try { ctx.storage.set('previewH', n) } catch {} }
  const [viewMode, setViewModeState] = useState(() => ctx.storage.get('viewMode', 'grid'))
  const setViewMode = v => { setViewModeState(v); try { ctx.storage.set('viewMode', v) } catch {} }
  const [tabs, setTabsState] = useState(() => {
    try {
      const saved = ctx.storage.get('tabs', null)
      if (Array.isArray(saved) && saved.length) {
        const clean = saved
          .filter(x => x && typeof x === 'object')
          .map((x, i) => ({ id: Number(x.id) || (Date.now() + i), cwd: typeof x.cwd === 'string' ? x.cwd : null }))
        if (clean.length) return clean.slice(0, 12)
      }
    } catch {}
    return [{ id: 1, cwd: null }]
  })
  const [activeId, setActiveIdState] = useState(() => ctx.storage.get('activeTab', null))
  const saveTabs = next => { setTabsState(next); try { ctx.storage.set('tabs', next) } catch {} }
  const setActive = id => { setActiveIdState(id); try { ctx.storage.set('activeTab', id) } catch {} }
  const active = tabs.find(x => x.id === activeId) || tabs[0]
  const addTab = () => {
    const id = Date.now()
    saveTabs([...tabs, { id, cwd: active?.cwd || home }].slice(-12))
    setActive(id)
  }
  const closeTab = id => {
    const i = tabs.findIndex(x => x.id === id)
    if (i < 0) return
    if (tabs.length <= 1) {
      const nid = Date.now()
      saveTabs([{ id: nid, cwd: home }])
      setActive(nid)
      return
    }
    const next = tabs.filter(x => x.id !== id)
    saveTabs(next)
    if (id === active.id) setActive(next[Math.min(i, next.length - 1)].id)
  }
  const [dual, setDualState] = useState(() => { try { return ctx.storage.get('dual', false) === true } catch { return false } })
  const setDual = v => { setDualState(v); try { ctx.storage.set('dual', v) } catch {} }
  const [dualCwd, setDualCwdState] = useState(() => { try { return ctx.storage.get('dualCwd', null) } catch { return null } })
  const setDualCwd = c => { setDualCwdState(c); try { ctx.storage.set('dualCwd', c) } catch {} }
  const paneProps = { ctx, sort, setSort, tilePx, setTilePx, showHidden, setShowHidden, sideWidth, setSideWidth, sideCollapsed, setSideCollapsed, sideOrder, setSideOrder, previewH, setPreviewH, viewMode, setViewMode }
  return jsxs('div', { className: 'hermes-fb', style: { flexDirection: 'column' }, children: [
    jsxs('div', { className: 'hermes-fb-tabs', children: [
      ...tabs.map(x => jsxs('button', {
        className: 'hermes-fb-tab', 'data-on': x.id === active.id, title: x.cwd || 'Home',
        onClick: () => setActive(x.id),
        children: [
          jsx('span', { children: tabName(x.cwd) }),
          tabs.length > 1 && jsx('span', { className: 'hermes-fb-tabx', title: 'Close tab', onClick: e => { e.stopPropagation(); closeTab(x.id) }, children: '×' }),
        ],
      }, x.id)),
      jsx('button', { className: 'hermes-fb-tbtn', title: 'New tab', onClick: addTab, children: '+' }),
      jsx('button', { className: 'hermes-fb-tbtn', title: t('dualPane'), style: dual ? { color: 'var(--ui-accent)' } : undefined, onClick: () => setDual(!dual), children: '◫' }),
      jsx('span', { style: { flex: 1 } }),
      jsx('button', { className: 'hermes-fb-tbtn', title: t('collapse'), onClick: () => { collapsed.set(true); syncPane() }, children: '−' }),
    ] }),
    ...(dual ? [jsxs('div', { style: { flex: 1, minHeight: 0, display: 'flex' }, children: [
      jsx(TabPane, { ...paneProps, tabId: active.id, initialCwd: active.cwd, hidden: false }, active.id),
      jsx('div', { style: { width: 1, background: 'var(--ui-stroke-secondary)', flexShrink: 0 } }),
      jsx(TabPane, { ...paneProps, tabId: 'dual', initialCwd: dualCwd || home, hidden: false, onCwd: setDualCwd }, 'dual'),
    ] })] : tabs.map(x => jsx(TabPane, {
      ctx, tabId: x.id, initialCwd: x.cwd, sort, setSort, tilePx, setTilePx, showHidden, setShowHidden,
      sideWidth, setSideWidth, sideCollapsed, setSideCollapsed, sideOrder, setSideOrder,
      previewH, setPreviewH, viewMode, setViewMode,
      hidden: x.id !== active.id,
    }, x.id))),
  ] })
}

function FileChip({ ctx }) {
  const t = usePluginI18n(ID)
  const isCollapsed = useValue(collapsed)
  return jsx(Tip, { label: t(isCollapsed ? 'expand' : 'collapse'), children: jsx(Button, {
    variant: 'ghost', size: 'micro',
    'aria-label': t(isCollapsed ? 'expand' : 'collapse'),
    onClick: () => { collapsed.set(!collapsed.get()); syncPane() },
    children: jsx('span', { style: { fontSize: 11 }, children: 'FileBox' }),
  }) })
}

return { Explorer, CSS, EN }
})()

/* ======================================================= system monitor
 *
 * Ported from the standalone monitor. `Panel` is the full view; Athena mounts
 * it in a tab instead of the statusbar.
 */
const AthenaSystem = (() => {
const ID = 'athena'
const POLL_MS = 3000

const EN = {
  sysmon: 'System monitor',
  open: 'Open system monitor',
  cpu: 'CPU', mem: 'RAM', gpu: 'GPU', vram: 'VRAM', disk: 'Disk',
  up: 'Up', down: 'Down', uptime: 'Uptime',
  top: 'Top processes', gpuProcs: 'On GPU',
  backendDown: 'Backend unreachable — restart Hermes to remount Athena.',
}
const LOCALES = { en: EN }

const CSS = `
.hermes-sysmon-bar{display:flex;align-items:center;gap:2px;height:100%;color:var(--ui-text-tertiary)}
.hermes-sysmon-chip{font-size:11px;white-space:nowrap;font-variant-numeric:tabular-nums;display:inline-block;min-width:8ch}
.hermes-sysmon-chip-gpu{min-width:12ch}
.hermes-sysmon-panel{width:300px;max-width:calc(100vw - 24px)}
.hermes-sysmon-head{display:flex;align-items:center;gap:6px;padding:2px 4px 6px;font-size:12px;font-weight:600;color:var(--ui-text-primary)}
.hermes-sysmon-meter{padding:3px 4px}
.hermes-sysmon-meter-top{display:flex;justify-content:space-between;font-size:11px;color:var(--ui-text-secondary)}
.hermes-sysmon-meter-top b{color:var(--ui-text-primary);font-weight:600}
.hermes-sysmon-track{height:4px;border-radius:2px;background:var(--chrome-action-hover);margin-top:3px;overflow:hidden}
.hermes-sysmon-fill{height:100%;background:var(--ui-accent);border-radius:2px}
.hermes-sysmon-fill[data-hot=true]{background:var(--ui-red)}
.hermes-sysmon-row{display:flex;justify-content:space-between;font-size:11px;color:var(--ui-text-secondary);padding:2px 4px}
.hermes-sysmon-row b{color:var(--ui-text-primary);font-weight:600}
.hermes-sysmon-group{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--ui-text-quaternary);padding:6px 4px 2px}
.hermes-sysmon-error{font-size:11px;line-height:16px;color:var(--ui-text-secondary);padding:4px}
.hermes-sysmon-net-head{display:flex;justify-content:space-between;gap:8px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--ui-text-quaternary);padding:8px 4px 2px}
.hermes-sysmon-net-head b{color:var(--ui-text-secondary);font-weight:600;letter-spacing:0;text-transform:none}
.hermes-sysmon-net-rate{display:flex;justify-content:space-between;gap:8px;padding:2px 4px;font-size:12px;color:var(--ui-text-primary)}
.hermes-sysmon-net-rate b{font-variant-numeric:tabular-nums;font-weight:600}
.hermes-sysmon-net-ar{color:var(--ui-text-quaternary);margin-right:5px}
.hermes-sysmon-net-total{display:flex;justify-content:space-between;gap:8px;font-size:11px;color:var(--ui-text-secondary);padding:4px 4px 0}
.hermes-sysmon-net-total b{color:var(--ui-text-primary);font-weight:600;font-variant-numeric:tabular-nums}
.hermes-sysmon-net-peak{font-size:10px;color:var(--ui-text-quaternary);padding:1px 4px 0}
.hermes-sysmon-net-nic{display:flex;justify-content:space-between;gap:8px;font-size:11px;color:var(--ui-text-secondary);padding:2px 4px}
.hermes-sysmon-net-nic b{color:var(--ui-text-primary);font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
.hermes-sysmon-net-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hermes-sysmon-spark{display:block;width:100%;height:26px;padding:3px 0 0}

`

function fmtBps(v) {
  v = Number(v) || 0
  if (v >= 1048576) return `${(v / 1048576).toFixed(1)} MB/s`
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KB/s`
  return `${v} B/s`
}

function fmtUptime(bootEpoch) {
  if (!bootEpoch) return '—'
  let s = Math.max(0, Math.floor(Date.now() / 1000 - bootEpoch))
  const d = Math.floor(s / 86400)
  s -= d * 86400
  const h = Math.floor(s / 3600)
  s -= h * 3600
  const m = Math.floor(s / 60)
  if (d) return `${d}d ${h}h`
  if (h) return `${h}h ${m}m`
  return `${m}m`
}

function Meter({ label, pct, text, hot }) {
  const v = Math.max(0, Math.min(100, Number(pct) || 0))
  return jsxs('div', { className: 'hermes-sysmon-meter', children: [
    jsxs('div', { className: 'hermes-sysmon-meter-top', children: [
      jsx('span', { children: label }),
      jsx('b', { children: text ?? `${Math.round(v)}%` }),
    ] }),
    jsx('div', { className: 'hermes-sysmon-track', children:
      jsx('div', { className: 'hermes-sysmon-fill', 'data-hot': !!hot, style: { width: `${v}%` } }),
    }),
  ] })
}

/* Totals arrive in MB, which is the unit a running total is read in. */
function fmtMB(mb) {
  const v = Number(mb) || 0
  if (v >= 1024) return (v / 1024).toFixed(1) + ' GB'
  if (v >= 1) return v.toFixed(0) + ' MB'
  return (v * 1024).toFixed(0) + ' KB'
}

/* The last few minutes as a shape. Two lines, the same scale, so down and up are
   comparable instead of each being normalised against itself — a 200 KB/s upload
   should not look like a saturating download. Colours are theme variables; only
   peak-finding is arithmetic. */
function NetSpark({ history }) {
  const rows = Array.isArray(history) ? history.slice(-120) : []
  if (rows.length < 3) return null
  const W = 100, H = 24
  let peak = 1
  for (const r of rows) peak = Math.max(peak, Number(r[0]) || 0, Number(r[1]) || 0)
  const line = (i) => rows.map((r, x) => {
    const v = Math.max(0, Number(r[i]) || 0)
    return `${((x / (rows.length - 1)) * W).toFixed(2)},${(H - 1 - (v / peak) * (H - 3)).toFixed(2)}`
  }).join(' ')
  return jsxs('svg', {
    className: 'hermes-sysmon-spark', viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: 'none', role: 'img',
    'aria-label': `Network throughput over the last ${rows.length} samples`,
    children: [
      jsx('polyline', { points: line(0), fill: 'none', stroke: 'var(--ui-accent)',
        strokeWidth: '1.3', vectorEffect: 'non-scaling-stroke' }),
      jsx('polyline', { points: line(1), fill: 'none', stroke: 'currentColor',
        strokeOpacity: '0.5', strokeWidth: '1', vectorEffect: 'non-scaling-stroke' }),
    ],
  })
}

/* Network, as a block: current rate per direction, a bar against the link the
   traffic is actually on, the shape of the last few minutes, what has moved since
   boot, and the adapters when there is more than one to tell apart. */
function NetPanel({ net }) {
  const n = net || {}
  const nics = Array.isArray(n.nics) ? n.nics : []
  const down = Number(n.down_bps) || 0
  const up = Number(n.up_bps) || 0
  const speed = Number(n.speed_mbps) || 0
  // Megabits against megabits: bytes/s * 8 / 1e6. A percentage against a
  // nominal link speed is the only way to see that 90 MB/s is not the same
  // thing on Wi-Fi as it is on a 10 GbE port.
  const pct = speed ? Math.min(100, ((down + up) * 8) / (speed * 1e6) * 100) : 0
  const first = nics[0]
  return jsxs('div', { children: [
    jsxs('div', { className: 'hermes-sysmon-net-head', children: [
      jsx('span', { children: 'Network' + (first && first.name ? ' · ' + first.name : '') }),
      jsx('b', { children: speed >= 1000 ? (speed / 1000).toFixed(1) + ' Gb/s'
                       : speed ? speed + ' Mb/s' : '—' }),
    ] }),
    jsxs('div', { className: 'hermes-sysmon-net-rate', children: [
      jsxs('div', { children: [
        jsx('span', { className: 'hermes-sysmon-net-ar', children: '↓' }),
        jsx('b', { children: fmtBps(down) }),
      ] }),
      jsxs('div', { children: [
        jsx('span', { className: 'hermes-sysmon-net-ar', children: '↑' }),
        jsx('b', { children: fmtBps(up) }),
      ] }),
    ] }),
    speed ? jsx('div', { className: 'hermes-sysmon-track', children:
      jsx('div', { className: 'hermes-sysmon-fill', 'data-hot': pct >= 85,
        style: { width: pct + '%' } }) }) : null,
    jsx(NetSpark, { history: n.history }),
    jsxs('div', { className: 'hermes-sysmon-net-total', children: [
      jsx('span', { children: 'Since boot' }),
      jsx('b', { children: '↓ ' + fmtMB(n.recv_mb) + ' ↑ ' + fmtMB(n.sent_mb) }),
    ] }),
    (n.peak_down_bps || n.peak_up_bps) ? jsx('div', { className: 'hermes-sysmon-net-peak', children:
      'peak ↓ ' + fmtBps(n.peak_down_bps) + '  ↑ ' + fmtBps(n.peak_up_bps) }) : null,
    nics.length > 1 ? jsxs('div', { children: nics.slice(0, 4).map((x) =>
      jsxs('div', { className: 'hermes-sysmon-net-nic', key: x.name, children: [
        jsx('span', { className: 'hermes-sysmon-net-name', title: x.name, children: x.name }),
        jsx('b', { children: '↓ ' + fmtBps(x.down_bps) + '  ↑ ' + fmtBps(x.up_bps) }),
      ] }) ) }) : null,
  ] })
}

function useStats(ctx) {
  return useQuery({
    queryKey: [ID, 'stats'],
    queryFn: async ({ signal }) => ctx.rest('/stats', { timeoutMs: 8000, signal }),
    refetchInterval: POLL_MS,
    staleTime: POLL_MS,
    retry: false,
  })
}

function Panel({ ctx }) {
  const t = usePluginI18n(ID)
  const q = useStats(ctx)
  const d = q.data ?? {}
  return jsxs('div', { children: [
    jsx('div', { className: 'hermes-sysmon-head', children: t('sysmon') }),
    q.isError && jsx('div', { className: 'hermes-sysmon-error', role: 'status', children: t('backendDown') }),
    !q.isError && jsxs('div', { children: [
      jsx(Meter, { label: `${t('cpu')}${d.cpu?.cores ? ` · ${d.cpu.cores}c` : ''}${d.cpu?.freq_mhz ? ` · ${(d.cpu.freq_mhz / 1000).toFixed(1)}GHz` : ''}`, pct: d.cpu?.percent, hot: (d.cpu?.percent ?? 0) >= 90 }),
      jsx(Meter, { label: t('mem'), pct: d.mem?.percent, text: d.mem ? `${d.mem.used_gb}/${d.mem.total_gb} GB` : undefined, hot: (d.mem?.percent ?? 0) >= 90 }),
      d.gpu && jsx(Meter, { label: `${t('gpu')}${d.gpu.temp_c != null ? ` · ${Math.round(d.gpu.temp_c)}°C` : ''}`, pct: d.gpu.util, hot: (d.gpu.temp_c ?? 0) >= 83 }),
      d.gpu && jsx(Meter, { label: t('vram'), pct: d.gpu.mem_total_mib ? (d.gpu.mem_used_mib / d.gpu.mem_total_mib) * 100 : 0, text: `${(d.gpu.mem_used_mib / 1024).toFixed(1)}/${(d.gpu.mem_total_mib / 1024).toFixed(1)} GB` }),
      ...(d.disks || []).map((dk) => jsx(Meter, { key: dk.mount, label: `${t('disk')} ${dk.mount}`, pct: dk.percent, text: `${dk.used_gb}/${dk.total_gb} GB` })),
      jsx(NetPanel, { net: d.net }),
      jsxs('div', { className: 'hermes-sysmon-row', children: [
        jsx('b', { children: `${t('uptime')} ${fmtUptime(d.boot_epoch)}` }),
      ] }),
      d.gpu?.procs?.length > 0 && jsxs('div', { children: [
        jsx('div', { className: 'hermes-sysmon-group', children: t('gpuProcs') }),
        jsx('div', { className: 'hermes-sysmon-row', children: jsx('span', { children: d.gpu.procs.join(' · ') }) }),
      ] }),
      d.top?.length > 0 && jsxs('div', { children: [
        jsx('div', { className: 'hermes-sysmon-group', children: t('top') }),
        ...d.top.map(p => jsxs('div', { className: 'hermes-sysmon-row', children: [
          jsx('span', { children: p.name }),
          jsx('b', { children: `${p.mem}%` }),
        ] }, p.name)),
      ] }),
    ] }),
  ] })
}

function SysBar({ ctx }) {
  const t = usePluginI18n(ID)
  const [open, setOpen] = useState(false)
  const q = useStats(ctx)
  const d = q.data ?? {}
  const cpuTxt = q.isFetching && d.cpu == null ? '…' : `${Math.round(d.cpu?.percent ?? 0)}%`
  const gpuTxt = d.gpu ? `${Math.round(d.gpu.util)}%${d.gpu.temp_c != null ? ` ${Math.round(d.gpu.temp_c)}°` : ''}` : null
  return jsxs('div', { className: 'hermes-sysmon-bar', children: [
    jsxs(Popover, { open, onOpenChange: setOpen, children: [
      jsx(PopoverTrigger, { asChild: true, children: jsxs(Button, {
        variant: 'ghost', size: 'micro',
        'aria-label': t('open'),
        children: [
          jsx('span', { className: 'hermes-sysmon-chip', children: `CPU ${cpuTxt}` }),
          gpuTxt && jsx('span', { className: 'hermes-sysmon-chip hermes-sysmon-chip-gpu', children: `GPU ${gpuTxt}` }),
          q.isFetching && d.cpu == null && jsx(GlyphSpinner, { ariaLabel: t('sysmon') }),
        ],
      }) }),
      // Host focus restoration must not dismiss a panel opened by a click.
      jsx(PopoverContent, { onFocusOutside: e => e.preventDefault(), side: 'top', align: 'end', className: 'hermes-sysmon-panel', 'aria-label': t('sysmon'), children:
        jsx(Panel, { ctx }),
      }),
    ] }),
  ] })
}

return { Panel, CSS, EN, NetSpark }   // NetSpark: the network chart; Athena's own mini monitor draws this same one
})()

export default {
  id: ID,
  name: 'Athena',
  description: 'The local AI app store: browse, install and run AI apps natively in Hermes.',
  dispose() {
    // The window's registration is not ctx-tracked (host.openWorkspace owns
    // it), so unmounting has to close it or the pane outlives the surface
    // that renders it.
    closeAthenaWindow()
    athenaCtx = null
    try { if (paneDispose) paneDispose() } catch { /* noop */ }
    try { if (viewerDispose) viewerDispose() } catch { /* noop */ }
    paneDispose = null
    viewerDispose = null
  },
  register(ctx) {
    const style = document.createElement('style')
    style.textContent = CSS
    document.head.append(style)
    athenaCtx = ctx
    ctx.onDispose(() => { style.remove() })
    // The ported halves bring their own stylesheets and strings; they used to
    // own a plugin each, so neither had to avoid the other's names.
    for (const extra of [AthenaFiles.CSS, AthenaSystem.CSS]) {
      const s = document.createElement('style')
      s.textContent = extra
      document.head.append(s)
      ctx.onDispose(() => { s.remove() })
    }
    try {
      ctx.i18n.register({ en: { ...AthenaFiles.EN, ...AthenaSystem.EN } })
    } catch { /* noop */ }
    // The deep monitor lives in its own window, opened from the mini monitor.
    // The ported panel is registered from here because only the merged half can
    // name it, and the ctx is memoised because the panel takes ctx in its hook
    // dependencies: a fresh object each render would re-fire its effects, and
    // every re-fire is another poll.
    let sysCtxKey = null
    let sysCtxVal = null
    registerSystemPanel((c) => {
      if (sysCtxKey !== c) { sysCtxKey = c; sysCtxVal = subCtx(c, '/sys', 'sys') }
      return jsx(AthenaSystem.Panel, { ctx: sysCtxVal })
    })
    // Warm the engine at launch instead of at the first click. A cold boot is
    // about six seconds of a port that accepts and says nothing, and until now
    // it only began when the pane (or the chip) first asked — so opening Athena
    // paid the whole wait. One GET, fire-and-forget: the engine's own autostart
    // is what this triggers, the same thing the first poll would have, and a
    // failure here is nothing to report because the poll will make the same call
    // a second later anyway.
    try { const p = ctx.rest('/status', { timeoutMs: 20000 }); if (p && p.catch) p.catch(() => {}) } catch { /* noop */ }
    // Seeded once here; the atoms own them from now on, so a Settings toggle
    // takes effect immediately rather than at the next reload.
    try { sysBar.set(ctx.storage.get('sysBar', true) !== false) } catch { /* noop */ }
    if (hasWorkspaceApi()) {
      // One window, opened here and closable from the chip.
      openAthenaWindow()
    } else {
      // Desktop too old for host.openWorkspace: the panes stay registered for
      // the life of the plugin and the chip can only front them.
      paneDispose = ctx.register({
        id: 'athena-pane', area: 'panes', title: 'Athena',
        data: { placement: 'right', width: '620px' },
        render: () => jsx(Panel, { ctx }),
      })
      // The viewer is its own pane rather than a tab inside the manager: an app's
      // own interface is the thing you look at while it runs, and it wants the
      // room, not a 620px column shared with the catalogue.
      viewerDispose = ctx.register({
        id: 'athena-viewer', area: 'panes', title: 'App',
        data: { placement: 'right', width: '1000px' },
        render: () => jsx(Viewer, { ctx }),
      })
    }
    ctx.register({
      id: 'athena-chip', area: STATUSBAR_AREAS.right, order: 15,
      render: () => jsx(StatusChip, { ctx }),
    })
    // The minimised monitor. Order 11.5 is deliberate: the right-hand side
    // sorts ascending and renders in that order, so this lands directly beside
    // the music widget (11) and ahead of the video widget (12) rather than
    // after Athena's own chip.
    ctx.register({
      id: 'athena-minimon', area: STATUSBAR_AREAS.right, order: 11.5,
      render: () => jsx(MiniMonitor, { ctx }),
    })
    // A finished job is announced even when nobody is watching it. The card
    // that started one reports it too, with a message that says more than
    // "finished", so this skips the ids the panel is already polling: the
    // broadcast only speaks for the jobs whose pane was switched away from or
    // closed.
    if (host && typeof host.onEvent === 'function') {
      try {
        ctx.onDispose(host.onEvent('plugin.athena.job.done', ({ payload }) => {
          const p = payload || {}
          if (!p.id || watchedJobs.has(p.id)) return
          const what = p.label || p.kind || 'Job'
          notify(ctx, p.state === 'done' ? 'info' : 'error',
            p.state === 'done' ? what + ' finished' : what + ' failed')
        }))
        // An app the agent drove is not a job: it leaves a run record, and it
        // never passes through the job bookkeeping above. This is the notice for
        // the render that finished while the panel was on another tab — it names
        // what came out, and stays quiet for the four-second calls the agent is
        // watching anyway.
        ctx.onDispose(host.onEvent('plugin.athena.run.done', ({ payload }) => {
          const w = runDoneWords(payload)
          if (w.quiet) return
          notify(ctx, w.kind, w.text)
        }))
        // A guest the agent started, put on screen. Pressing Start in the panel opens
        // this tab already, because the panel is where the press happened; a guest
        // started from a chat has no such moment, so without this a machine an agent is
        // using comes up and stays invisible. Raising the screen is the whole point of
        // the tab — somebody who asked an agent to do something to a computer should get
        // to watch it, not be told afterwards that it happened. The server only sends
        // this for a machine it also put on the idle clock, so what appears here is
        // never a guest nobody is accounting for.
        ctx.onDispose(host.onEvent('plugin.athena.screen.wanted', ({ payload }) => {
          const p = payload || {}
          if (!p.machine) return
          watchMachine.set(p.machine)
          tab.set('mscreen')
        }))
        // The agent is using something in this panel, and the arc is how you see that without
        // reading a log. If the card is not on screen, the agent is working elsewhere in the
        // panel, so follow it to the tab that draws it — `screen.wanted` already does exactly
        // this for a machine's console, so it is established behaviour here, not a new liberty.
        ctx.onDispose(host.onEvent('plugin.athena.spot', ({ payload }) => {
          const p = payload || {}
          if (!p.key) return
          spotFollow(p.key, p.ms, (t) => tab.set(t))
        }))
      } catch { /* noop */ }
    }
  },
}

// ---- test surface ---------------------------------------------------------
// Appended by mk_ui_test.py. Named exports only: the default export stays the
// plugin itself, so the driver registers the same object the app loads.
export const __fns = {
  actionFailure,   // what a failed action says out loud, or nothing when it really succeeded,
  AppCard,   // an app's card: its state chip, the one button, and the menu behind,
  appStateWords,   // the one chip on a card: running · :7860, runs here, nothing read yet,
  appActions,   // what a card offers as its button, its second, and its menu, by state,
  keyBadge,   // what an app asks for, as the one badge the card shows,
  keysMatch,   // the apps-tab filter for what runs here and what wants a key,
  StoreCard,   // a store card, whose install outlives it,
  StoreTab,   // the store's own argument, and the filter for what carries a sheet,
  runDoneWords,   // what a finished run says, artifact included,
  runSecs,   // how a run's length is worded: 8s, 4m 5s, 2h 3m,
  jobPace,   // what a running job says: working, quiet for 40s, no output for 6m,
  jobGoneWords,   // what a poll says about a job that vanished with the engine: the backend's sentence, or the restart,
  updateAllWords,   // what the shelf says when a shelf-wide update finishes: how many, and where the reasons are,
  pollDelay,   // how long the panel waits before asking /status again: a second while the engine is coming up, five when it is settled,
  guestPoint,   // where a click on the panel's picture lands inside the guest,
  screenWords,   // one line above the guest's screen: its size, the frame's size, when it was read,
  screenLive,   // whether the panel should keep asking for frames at all,
  frameDelay,   // how long to wait before the next frame: quick while it moves, slower when it does not,
  kickDelay,   // how long input waits for the picture back: leave a look that is already due alone, bring a later one in,
  POINTER_MOVE_MS,   // how often hovering may move the guest's pointer,
  screenKey,   // which key name the guest gets told about,
  engineLine,   // the header's one line about the engine: ready with its port, starting, or stopped,
  Header,   // the panel's own chrome: the engine line and the Start/Stop/Restart that act on it,
  waited,   // how long the wait has been, as a word the header can put after starting…,
  keyOwnerStep,   // whether a key hands the keyboard back, or belongs to the guest — a value, so the gesture is provable,
  dockNotes,   // what the screen dock says about who holds the keyboard and how the pointer behaves,
  keyboardButton,   // the one click that changes who holds the keyboard,
  keyboardNote,   // the dock's line, whole,
  leaseWords,   // who is driving the guest's screen, and the one button that takes it back,
  putBackPanel,   // where an app has been, and what going back would cost,
  costLine,   // what the card has cost, in one sentence, with no claim it cannot back,
  costDetail,   // the small print: what was counted, what was not,
  costSpan,   // how a stretch of the card's time is worded: 1d 17h, 12m,
  GpuCostCard,   // the card's running total, and the price that prices it,
  kindWords,   // how a file kind is said out loud: an image, a 3D model,
  acceptsPanel,   // which apps here would take this file, and why the others would not,
  SendToApp,   // the bar under the file browser that hands a file to one of them,
  madePanel,   // what a run made, and who would take it on next,
  repeatPanel,   // what runs on its own, and whether it still will,
  RunsTab,   // the history of what has run here, with the repeats above it,
  JOB_QUIET_HINTS,   // what a silent job of each kind is usually doing,
  machineWords,   // the one chip on a machine: running, stopped, no distro yet,
  shellTabs,   // the tab bar: which tabs exist, and what a watched machine adds to them,
  newMachineRow,   // the first row of the make-a-machine form, drawn from what it is given,
  machineFields,   // a machine's settings as text a box can hold,
  machineChanges,   // only the differences between what was typed and what the machine is,
  portPairs,   // host:guest pairs, with the lines that are not pairs refused by name,
  MachineScreenTab,   // a machine's screen in its own tab, at the size of the pane,
  watchMachine,   // which machine's screen is open, if any,
  GuestScreen,   // the picture of a guest, and the input that goes back through it,
  bootChoice,   // where a machine with a medium attached starts, and the other way of it,
  INPUT_ECHO_MS,   // when to look after the person did something,
  INPUT_BURST_MS,   // how long a burst of typing keeps the fast cadence,
  INPUT_FRAME_MS,   // the fast cadence: just above the guest's own echo,
  STILL_FRAME_MS,   // nothing has happened for two frames,
  MOVING_FRAME_MS,   // something is happening that nobody asked for,
  machineLine,   // what a machine is, in one line: WSL, its distro, its cores,
  machinePorts,   // the ports a machine is reachable on, host→guest,
  parsePorts,   // "8080:80, 2222:22" as the spec wants it, junk dropped,
  isoWords,   // the image folder as one line: where it is and what is in it,
  isoLabel,   // a file in the ISO menu: name, what the name suggests, how big,
  isoOptions,   // the ISO menu itself — the folder's files, plus "somewhere else",
  isoGroups,   // images by the folder they live in, the top folder first,
  isoGroupsShown,   // which folders are showing: the top one open, subfolders shut until asked,
  groupMap,   // every folder open or every folder shut, for the one button that does both,
  needList,   // whatever a response says is missing, as a list of things to set up,
  folderWords,   // the folder browser's one line: where you are and what is in there,
  isoPatch,   // what picking a file fills in: path, kind, and a suggested guest OS,
  fitWords,   // the size this computer can give a guest, with the first reason,
  needWords,   // a missing WSL or QEMU as the question it turns into,
  SetupCard,   // the card that asks before anything is installed,
  FolderPicker,   // walking to the image folder instead of typing its path,
  JobProgress,   // the bar a job draws: bytes when there are bytes, a label when there are not,
  MachineCard,   // a machine's card: its chip, its verb, the console behind,
  rigBadge,   // whether anyone has this app working on a box like this one,
  rigMatch,   // the store filter for apps checked in from Windows on NVIDIA,
  RunsRow,   // one recorded run,
  AutoTerm,   // the auto-following output box,
  installWrite,   // write a store install's state,
  installRead,   // read a store install's state,
  startInstall,   // run an install with nothing mounted,
  AppsTab,   // the installed-apps tab, with its search and running list,
  appMatches,   // the apps-tab search filter, pure,
  HealthReport,   // the check list, and the button a step on one of them offers,
  WeightShelf,   // the shelf: what the disk holds, and pulling what it lacks,
  NeedsYou,   // what needs you: the report, in the System window,
  needsPanel,   // the report drawn from data, without a fetch behind it,
  spotSelector,   // the selector for the one thing the agent says it is touching, or nothing,
  spotTab,   // the tab that draws the kind of card an agent named, or ''
}
