"""Fold the file browser and the system monitor into Athena's desktop half.

Desktop plugins load uncompiled and may only import `@hermes/plugin-sdk`,
`react` and `react/jsx-runtime` — there are no relative imports, so the two
donor UIs cannot be imported. They have to live in the same file.

Both donors declare the same top-level names Athena does (`ID`, `CSS`,
`activeJob`, `paneDispose`), so concatenating them flat would collide. Each is
therefore wrapped in an IIFE that returns only what Athena needs, which keeps
every donor identifier in its own scope instead of renaming ~90 symbols and
hoping none was missed.

The donors call `ctx.rest('/list')` etc. against their own plugin prefix, so
each gets a thin adapter that prefixes paths (`/files`, `/sys`) and namespaces
`ctx.storage` keys — otherwise the browser's `tabs`/`viewMode` keys would
collide with Athena's own settings in the same store.
"""
import re
import shutil
import sys
from pathlib import Path

import os
# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUG = Path(str(HERMES_ROOT) + r"/plugins")
ATHENA = PLUG / "athena/desktop/plugin.js"
FILEBOX = PLUG / "hermes-filebox/desktop/plugin.js"
SYSMON = PLUG / "sysmon/desktop/plugin.js"

MARK = "from 'react/jsx-runtime'"


def body_after_imports(src: str) -> str:
    """Everything after the import block — the donor's module body."""
    i = src.index(MARK) + len(MARK)
    nl = src.index("\n", i)
    return src[nl + 1:].lstrip("\n")


def body_before_export(src: str) -> str:
    """The module body with the donor's own `export default` stripped."""
    i = src.index("\nexport default {")
    return src[:i].rstrip() + "\n"


def rename_id(body: str, old: str) -> str:
    """Point the donor's i18n id at Athena.

    `usePluginI18n(ID)` looks strings up by id, so the donor's own id would
    resolve to nothing and the UI would render raw keys.
    """
    out = body.replace(f"const ID = '{old}'", "const ID = 'athena'", 1)
    if out == body:
        raise SystemExit(f"could not retarget ID for {old}")
    return out


# Re-running this script must land on the same output, so start from the
# pristine file rather than merging a merge.
backup = ATHENA.with_suffix(".js.pre-merge")
if backup.exists() and "const AthenaFiles = (() => {" in ATHENA.read_text(encoding="utf-8"):
    ATHENA.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")

athena = ATHENA.read_text(encoding="utf-8")
fb_full = FILEBOX.read_text(encoding="utf-8")
sm_full = SYSMON.read_text(encoding="utf-8")

# --- donor bodies, scoped -------------------------------------------------
fb_body = rename_id(body_before_export(body_after_imports(fb_full)), "hermes-filebox")
sm_body = rename_id(body_before_export(body_after_imports(sm_full)), "sysmon")

# Athena's backend reports *every* volume, as a list (`disks`). The donor reads
# the single-volume `d.disk` its own older backend returned, so ported as-is
# that meter silently sat at 0% on every machine — a dead gauge, not a missing
# feature. Rewrite it to map the list, so a nearly-full drive names itself.
_DISK_OLD = ("      jsx(Meter, { label: t('disk'), pct: d.disk?.percent, "
             "text: d.disk ? `${d.disk.used_gb}/${d.disk.total_gb} GB` : undefined }),")
_DISK_NEW = ("      ...(d.disks || []).map((dk) => jsx(Meter, { key: dk.mount, "
             "label: `${t('disk')} ${dk.mount}`, pct: dk.percent, "
             "text: `${dk.used_gb}/${dk.total_gb} GB` })),")
if _DISK_OLD not in sm_body:
    raise SystemExit("donor disk meter not found — donor changed?")
sm_body = sm_body.replace(_DISK_OLD, _DISK_NEW, 1)
if "disk: 'Disk C:'" not in sm_body:
    raise SystemExit("donor disk label not found — donor changed?")
sm_body = sm_body.replace("disk: 'Disk C:'", "disk: 'Disk'", 1)

# --- sending a file to an app ---------------------------------------------
# The entry point a person actually uses for a file is the file's own right-click
# menu, and that menu belongs to the ported browser — which lives in its own
# scope and cannot call into Athena's half. So one menu item hands the path to a
# single global function that Athena's own panel installs while its Files tab is
# on screen. One line of donor surgery instead of a rewrite of its row rendering.
_FB_MENU_OLD = "    jsx(ContextMenuItem, { onSelect: () => quickOp('reveal', e), children: t('reveal') }),"
_FB_MENU_NEW = _FB_MENU_OLD + """
    !e.is_dir && !isZipV(e.path) && jsx(ContextMenuItem, { onSelect: () => { try { window.__athenaSendToApp && window.__athenaSendToApp(e.path) } catch { /* nothing on screen to receive it */ } }, children: 'Send to an app\\u2026' }),"""
if fb_body.count(_FB_MENU_OLD) != 1:
    raise SystemExit("donor reveal menu item not found — donor changed?")
fb_body = fb_body.replace(_FB_MENU_OLD, _FB_MENU_NEW, 1)

# --- network: rates, a shape, and where the traffic went --------------------
# The donor printed one row: a down rate, an up rate, and the uptime crammed in
# beside them. That answers "is it busy" and nothing else. Athena's backend now
# reports per-interface rates, totals since boot and the last few minutes of
# throughput, so the ported panel gets a block that reads like a network monitor
# instead of a pair of numbers.
_CSS_OLD = ".hermes-sysmon-error{font-size:11px;line-height:16px;color:var(--ui-text-secondary);padding:4px}"
_CSS_NEW = _CSS_OLD + """
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
"""
if sm_body.count(_CSS_OLD) != 1:
    raise SystemExit("donor error style not found — donor changed?")
sm_body = sm_body.replace(_CSS_OLD, _CSS_NEW, 1)

_NET_FNS = """/* Totals arrive in MB, which is the unit a running total is read in. */
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
      jsx('b', { children: '↓ ' + fmtMB(n.recv_mb) + '\u2003↑ ' + fmtMB(n.sent_mb) }),
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

"""
_NET_ANCHOR = "function useStats(ctx) {"
if sm_body.count(_NET_ANCHOR) != 1:
    raise SystemExit("donor useStats not found — donor changed?")
sm_body = sm_body.replace(_NET_ANCHOR, _NET_FNS + _NET_ANCHOR, 1)

_NET_ROW_OLD = """      jsxs('div', { className: 'hermes-sysmon-row', children: [
        jsx('span', { children: `↓ ${fmtBps(d.net?.down_bps)}` }),
        jsx('span', { children: `↑ ${fmtBps(d.net?.up_bps)}` }),
        jsx('b', { children: `${t('uptime')} ${fmtUptime(d.boot_epoch)}` }),
      ] }),"""
_NET_ROW_NEW = """      jsx(NetPanel, { net: d.net }),
      jsxs('div', { className: 'hermes-sysmon-row', children: [
        jsx('b', { children: `${t('uptime')} ${fmtUptime(d.boot_epoch)}` }),
      ] }),"""
if sm_body.count(_NET_ROW_OLD) != 1:
    raise SystemExit("donor network row not found — donor changed?")
sm_body = sm_body.replace(_NET_ROW_OLD, _NET_ROW_NEW, 1)

# The ported browser streams media itself instead of going through `ctx.rest`,
# so it builds ABSOLUTE urls: `${origin}/api/plugins/<donor>/dl`. That hardcodes
# the donor's mount, and the donor is gone — the route was ported and lives at
# Athena's own `/files/dl`. Both call sites (the <audio>/<video> stream and the
# save-to-disk link) 404'd. Neither the syntax check nor the route checker can
# see this: the checker only inspects `ctx.rest` paths, and a hand-built URL
# string is neither syntax nor a route reference.
_mount_old = "/api/plugins/hermes-filebox"
if fb_body.count(_mount_old) != 2:
    raise SystemExit(f"expected 2 donor mounts in the file browser, found {fb_body.count(_mount_old)}")
fb_body = fb_body.replace(_mount_old, "/api/plugins/athena/files")

# Both tabs told the user to go enable the donor plugin when their backend was
# unreachable. The donors are retired; the tabs are Athena's now. Same
# replacement reads correctly in the file browser and the system monitor.
_bd_old = "'Backend unreachable — enable "
for _label, _body in (("filebox", fb_body), ("sysmon", sm_body)):
    _i = _body.find("backendDown: " + _bd_old)
    if _i == -1:
        raise SystemExit(f"{_label}: backendDown string not found — donor changed?")
    _j = _body.index("',", _i) + 2
    _body = _body[:_i] + "backendDown: 'Backend unreachable — restart Hermes to remount Athena.'," + _body[_j:]
    if _label == "filebox":
        fb_body = _body
    else:
        sm_body = _body

# Drop the donor's self-updater. It posts to `/update-check` and `/update`,
# which were deliberately not ported — Athena has no self-update path, its apps
# and itself move through the catalogue — so leaving the chip in place would
# ship a button that only ever reports "check failed".
_i = fb_body.find("function UpdateChip({ ctx, t }) {")
if _i == -1:
    raise SystemExit("UpdateChip definition not found — donor changed?")
_j = fb_body.index("\n}\n", _i) + len("\n}\n")
fb_body = fb_body[:_i] + fb_body[_j:]
fb_body = re.sub(r"^\s*jsx\(UpdateChip,[^\n]*\n", "", fb_body, flags=re.M)
if "UpdateChip" in fb_body:
    raise SystemExit("UpdateChip still referenced after removal")

# ...and the strings that existed only for it. `checkingUpdate` stays: it is
# reused as a generic spinner label in the properties panel.
_before = len(re.findall(r"(?m)^\s*(upToDate|updateDone|aheadV|aheadTip)\s*:.*$", fb_body))
fb_body = re.sub(r"(?m)^\s*(upToDate|updateDone|aheadV|aheadTip)\s*:.*\n", "", fb_body)
if _before != 4:
    raise SystemExit(f"expected 4 updater strings to remove, found {_before}")

# Sanity: a donor body must not smuggle in an export or a second import.
for name, body in (("filebox", fb_body), ("sysmon", sm_body)):
    if re.search(r"^\s*(export|import)\b", body, re.M):
        raise SystemExit(f"{name} body still contains import/export")

FILES_BLOCK = f"""
/* ===================================================== app file browser
 *
 * Ported from the standalone file browser, re-scoped to Athena's home. Its own
 * prefix, storage namespace and i18n id come from the adapter below, so the
 * body is wrapped to keep its `ID`/`CSS`/`activeJob` from colliding with
 * Athena's identically-named module-level bindings.
 */
const AthenaFiles = (() => {{
{fb_body}
return {{ Explorer, CSS, EN }}
}})()
"""

SYSTEM_BLOCK = f"""
/* ======================================================= system monitor
 *
 * Ported from the standalone monitor. `Panel` is the full view; Athena mounts
 * it in a tab instead of the statusbar.
 */
const AthenaSystem = (() => {{
{sm_body}
return {{ Panel, CSS, EN, NetSpark }}   // NetSpark: the network chart; Athena's own mini monitor draws this same one
}})()
"""

ADAPTER = """
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
"""

# --- imports: union of all three -----------------------------------------
OLD_IMPORTS = """import {
  Button, SearchField, STATUSBAR_AREAS, Tip, atom, host, useValue
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'"""

NEW_IMPORTS = """import {
  Badge, Button, Codicon, ContextMenu, ContextMenuContent, ContextMenuItem,
  ContextMenuSeparator, ContextMenuTrigger, GlyphSpinner, ListRow, Popover,
  PopoverContent, PopoverTrigger, SearchField, STATUSBAR_AREAS, StatusDot,
  Tip, ToggleRow, atom, host, usePluginI18n, useQuery, useValue
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'"""
import os


if OLD_IMPORTS not in athena:
    raise SystemExit("athena import block not found — already merged?")
athena = athena.replace(OLD_IMPORTS, NEW_IMPORTS, 1)

# --- insert the adapter and the two scoped blocks ------------------------
anchor = "\nexport default {"
if anchor not in athena:
    raise SystemExit("athena export default not found")
athena = athena.replace(anchor, ADAPTER + FILES_BLOCK + SYSTEM_BLOCK + anchor, 1)

# --- tabs ----------------------------------------------------------------
OLD_TABS = "const tabs = [['apps', 'Apps'], ['store', 'Store'], ['viewer', 'App'], ['settings', 'Settings']]"
# Five app tabs plus Runs: the standalone System tab is gone (the mini monitor in
# the status bar was already the same reading, and the deep view is now a window
# opened from it). Runs is the record of what the agent has actually run here,
# which is how someone learns what the machine can do. There is no Logs tab --
# the terminal is opened by an app's own button and appears only while it is open
# (see the splice after this line in plugin.js.pre-merge).
# Machines sits beside Files: both are workbenches, and neither is a
# record of anything (Runs) or a view of one app (App).
NEW_TABS = "const tabs = [['apps', 'Apps'], ['store', 'Store'], ['files', 'Files'], " \
    "['machines', 'Machines'], ['viewer', 'App'], ['runs', 'Runs'], ['settings', 'Settings']]"
if OLD_TABS not in athena:
    raise SystemExit("tab list not found")
athena = athena.replace(OLD_TABS, NEW_TABS, 1)

# --- panel: donor contexts, memoised so hooks do not see a new object ----
OLD_PANEL = """function Panel({ ctx }) {
  const [st, err, refresh] = useEngine(ctx)
  const t = useValue(tab)"""
NEW_PANEL = """function Panel({ ctx }) {
  const [st, err, refresh] = useEngine(ctx)
  // Built once per ctx: the ported UIs take ctx in hook dependencies, so a
  // fresh object each render would re-fire their effects on every paint.
  const filesCtx = useMemo(() => subCtx(ctx, '/files', 'files'), [ctx])
  const t = useValue(tab)"""
if OLD_PANEL not in athena:
    raise SystemExit("Panel head not found")
athena = athena.replace(OLD_PANEL, NEW_PANEL, 1)

OLD_RENDER = "      t === 'store' ? jsx(StoreTab, { ctx, st, refresh }) : null,\n"
NEW_RENDER = OLD_RENDER + (
    # The ported browser keeps the pane and the send bar sits under it: one file
    # is routed at a time, and the answer to "who would take this" belongs beside
    # the file rather than in a window of its own. The bar takes the ROOT ctx --
    # it calls Athena's own routes, not the browser's.
    "      t === 'files' ? jsxs('div', { className: 'ah-files', children: [\n"
    "        jsx(AthenaFiles.Explorer, { ctx: filesCtx }),\n"
    "        jsx(SendToApp, { ctx }),\n"
    "      ] }) : null,\n"
    "      t === 'runs' ? jsx(RunsTab, { ctx }) : null,\n"
    "      t === 'machines' ? jsx(MachinesTab, { ctx, st }) : null,\n"
)
if OLD_RENDER not in athena:
    raise SystemExit("render chain not found")
athena = athena.replace(OLD_RENDER, NEW_RENDER, 1)

# --- register(): inject the ported stylesheets and strings ---------------
OLD_STYLE = """    const style = document.createElement('style')
    style.textContent = CSS
    document.head.append(style)
    athenaCtx = ctx
    ctx.onDispose(() => { style.remove() })"""
NEW_STYLE = """    const style = document.createElement('style')
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
    })"""
if OLD_STYLE not in athena:
    raise SystemExit("register style block not found")
athena = athena.replace(OLD_STYLE, NEW_STYLE, 1)

# ---------------------------------------------------------------- theming
# The app expresses its palette as --ui-* / --dt-* tokens. The friendly names
# the ported UIs were written against (--card, --border, --accent,
# --muted-foreground) are aliased only inside the *preview widget* prelude —
# the app's inline-preview-directive carries a THEME_BRIDGE_TOKENS table that
# maps exactly those five names for widget HTML. A plugin pane never gets that
# prelude, and an undefined var() invalidates the whole declaration at
# computed-value time: borders fell back to currentColor and every "muted"
# subtitle rendered at full foreground strength. Re-point them at the tokens a
# pane actually inherits.
def retheme(text: str) -> tuple[str, list[str]]:
    log: list[str] = []

    def swap(old: str, new: str, expect: int | None = None) -> None:
        nonlocal text
        n = text.count(old)
        if n == 0 or (expect is not None and n != expect):
            raise SystemExit(f"retheme: wanted {expect} x {old!r}, found {n}")
        text = text.replace(old, new)
        log.append(f"  x{n:<3d} {old[:44]:<44} -> {new[:40]}")

    def swap_re(pat: str, new: str) -> None:
        nonlocal text
        rx = re.compile(pat)
        n = len(rx.findall(text))
        if n == 0:
            raise SystemExit(f"retheme: nothing matched /{pat}/")
        text = rx.sub(new, text)
        log.append(f"  x{n:<3d} /{pat[:42]:<42} -> {new[:40]}")

    # 1. `--accent` is the user's *saturated* brand colour. Right for emphasis,
    #    wrong for a hover wash (a full-strength blue fill), so each site is
    #    named rather than blanket-replaced: hovers take the app's subtle
    #    control-hover tint, placeholder tiles take a low-alpha fill.
    hover = "background:var(--chrome-action-hover)"
    tile = "background:var(--ui-bg-secondary)"
    rad6 = "border-radius:calc(var(--radius-scalar) * .375rem)"
    for old, new in [
        (".ah-tabb:hover { color:var(--foreground); background:var(--accent); }",
         f".ah-tabb:hover {{ color:var(--ui-text-primary); {hover}; }}"),
        (".ah-choice:hover { background:var(--accent); }",
         f".ah-choice:hover {{ {hover}; }}"),
        (".ah-btn:hover:not(:disabled) { background:var(--accent); }",
         f".ah-btn:hover:not(:disabled) {{ {hover}; }}"),
        (".ah-ico { width:28px; height:28px; border-radius:6px; object-fit:cover; flex:0 0 auto; background:var(--accent); }",
         f".ah-ico {{ width:28px; height:28px; {rad6}; object-fit:cover; flex:0 0 auto; {tile}; }}"),
        (".ah-icoph { width:28px; height:28px; border-radius:6px; flex:0 0 auto; background:var(--accent); display:flex; align-items:center; justify-content:center; color:var(--muted-foreground); }",
         f".ah-icoph {{ width:28px; height:28px; {rad6}; flex:0 0 auto; {tile}; display:flex; align-items:center; justify-content:center; color:var(--ui-text-tertiary); }}"),
    ]:
        swap(old, new, 1)

    # 2. the four names a pane does not define, plus the two widget aliases.
    swap("var(--muted-foreground)", "var(--ui-text-tertiary)")
    swap("var(--border)", "var(--ui-stroke-tertiary)")
    swap("var(--card)", "var(--ui-bg-editor)")
    swap("var(--foreground)", "var(--ui-text-primary)")
    swap("var(--background)", "var(--ui-bg-chrome)")

    # 3. literal palette colours -> the app's semantic set. The trailing
    #    lookahead stops `#22c55e` from clipping `#22c55e88` into a stray "88".
    for lit, tok in (
        ("#22c55e88", "color-mix(in srgb, var(--ui-green) 53%, transparent)"),
        ("#22c55e66", "color-mix(in srgb, var(--ui-green) 40%, transparent)"),
        ("#22c55e", "var(--ui-green)"),
        ("#ef4444", "var(--ui-red)"),
        ("#e5484d", "var(--ui-red)"),
        ("#f59e0b", "var(--ui-yellow)"),
        ("#dcb959", "var(--ui-yellow)"),
        ("#94a3b8", "var(--ui-text-quaternary)"),
        ("#ddd", "var(--ui-text-secondary)"),
        ("#bbb", "var(--ui-text-tertiary)"),
        ("#888", "var(--ui-text-tertiary)"),
    ):
        swap_re(re.escape(lit) + r"(?![0-9a-fA-F])", tok)

    # 4. type and corner geometry follow the app instead of a literal stack.
    swap("ui-monospace,Menlo,Consolas,monospace", "var(--font-mono)")
    swap("border-radius:8px", "border-radius:var(--radius-md)")
    swap("border-radius:6px", "border-radius:calc(var(--radius-scalar) * .375rem)")
    swap("border-radius:4px", "border-radius:calc(var(--radius-scalar) * .25rem)")

    # 5. the 3D viewport: the canvas paints via the 2D API (cannot read a custom
    #    property), so its fill is resolved off the document; the element behind
    #    it takes the token directly so the two agree.
    swap("  g.fillStyle = '#141419'",
         "  g.fillStyle = themeInk('--ui-bg-editor', '#141419')", 1)
    swap("background: '#141419'", "background: 'var(--ui-bg-editor)'", 1)
    return text, log


athena, _theme_log = retheme(athena)
print("theming pass:")
for _line in _theme_log:
    print(_line)

# A retired donor's mount must never survive into the output. A ported tab that
# streams media builds an absolute URL, and no other check sees one — the route
# checker inspects `ctx.rest` paths only. Assert it here, at the single door
# every merge passes through, so a future donor can't smuggle one back in.
_stray = sorted({m for m in re.findall(r"/api/plugins/([a-zA-Z0-9._-]+)", athena) if m != "athena"})
if _stray:
    raise SystemExit(f"plugin.js references foreign plugin mounts: {_stray}")
print(f"mount check: {len(re.findall(r'/api/plugins/', athena))} plugin url(s), all Athena's own")

# What has to be in the output, and what must never come back. The negatives are
# contracts too: the standalone System tab was removed on purpose — the mini
# monitor was already the same reading — and a merge that quietly restored it
# would otherwise look perfectly healthy here.
_EXPECT = ("const AthenaFiles = (() => {", "const AthenaSystem = (() => {",
           "function subCtx(", "['files', 'Files']", "t === 'files'",
           "['runs', 'Runs']", "t === 'runs'", "function RunsTab(", ".ah-runrow",
           "['machines', 'Machines']", "t === 'machines'", "function MachinesTab(",
           "function machineWords(", "function SetupCard(", "function MachineCard(",
           "ah-running", "still going",
           "function SystemWindow(", "registerSystemPanel(",
           "host.openWorkspace(SYSTEM_WINDOW_ID", "plugin.athena.job.done",
           "function Section(", "openSystemWindow", "useSettings(",
           # A terminal that follows its output, and an install that outlives the
           # card that started it: the store record lives at module scope, so
           # leaving the tab cannot lose a build that is still running.
           # One file in hand, and the apps that would take it: the donor menu
           # item is the door, the panel is Athena's own.
           "Send to an app", "function acceptsPanel(", "function SendToApp(",
           "window.__athenaSendToApp", "ah-send-row",
           "function AutoTerm(", "const INSTALLS = new Map()",
           "function useInstall(", "async function startInstall(")
_FORBID = ("['system', 'System']", "t === 'system'", "function SystemTab(",
           "sysMin", "setSysMin", "openSystemTab",
           # Every terminal in the UI goes through AutoTerm now. A raw <pre> with
           # the terminal class is one that has quietly stopped scrolling.
           "jsx('pre', { className: 'ah-term'")
_missing = [t for t in _EXPECT if t not in athena]
_present = [t for t in _FORBID if t in athena]
for token in _EXPECT:
    print(f"  {'ok ' if token in athena else 'MISSING'} {token}")
for token in _FORBID:
    print(f"  {'ok  absent ' if token not in athena else 'PRESENT'} {token}")
if _missing or _present:
    raise SystemExit(f"merge contract failed: missing={_missing} unwanted={_present}")

backup = ATHENA.with_suffix(".js.pre-merge")
if not backup.exists():
    shutil.copy2(ATHENA, backup)
ATHENA.write_text(athena, encoding="utf-8")

lines = athena.count("\n") + 1
print(f"wrote {ATHENA}")
print(f"  {len(athena):,} bytes, {lines:,} lines  (backup: {backup.name})")
print(f"  filebox body : {len(fb_body):,} bytes")
print(f"  sysmon body  : {len(sm_body):,} bytes")
