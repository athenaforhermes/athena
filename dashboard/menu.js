// Resolve an app's launcher menu into a flat list of runnable choices.
//
// Why this exists: an app can put all its runnable entries *inside* submenus and
// set no `default` at the top level — Hunyuan3D-2-LowVRAM does exactly that, and
// its `start.js` builds `--profile {{args.profile}}` with no fallback, so a bare
// launch cannot work. The engine resolves the menu to *render* it but exposes no
// route that returns it as data, so Athena resolves it here, the same way the
// engine does: load the launcher, call menu(kernel, info).
//
//   node menu.js <appDir> [contextJSON]
//
// contextJSON may carry {"running": {"start.js": true}, "urls": {"start.js": "http://..."}}
// so `info.running()` / `info.local()` answer truthfully and an installed, running
// app resolves to its "Open Web UI" entry.

const fs = require('fs')
const path = require('path')

function makeInfo(appDir, ctx) {
  const running = (ctx && ctx.running) || {}
  const urls = (ctx && ctx.urls) || {}
  const key = (p) => path.basename(String(p || ''))
  return {
    exists: (...parts) => fs.existsSync(path.resolve(appDir, ...parts.map(String))),
    running: (p) => !!running[key(p)],
    local: (p) => (urls[key(p)] ? { url: urls[key(p)] } : {}),
    // A few apps reach for these; absent ones must not throw.
    ready: () => false,
    info: {},
  }
}

// Apps call kernel methods their menu does not need. Known values are real;
// anything else must read as *absent* rather than as a stub, because menus use
// genuinely optional namespaces as feature gates — `if (kernel.jsdom) { … }`
// (chatbot-ollama) sent the stub down a path whose real implementation is not
// here and threw. An undefined read degrades exactly like a missing feature.
function makeKernel(info, appDir) {
  const base = {
    platform: process.platform,
    homedir: appDir,
    info,
    running: info.running,
    exists: info.exists,
    local: info.local,
    path: (...p) => path.resolve(appDir, ...p),
    json: {},
    // Menus do read these back (e.g. `kernel.memory.local[<script>]`).
    memory: { local: {}, args: {}, input: {}, rpc: {} },
    port: async () => 0,
  }
  return new Proxy(base, {
    get(t, prop) { return prop in t ? t[prop] : undefined },
    has: (t, prop) => prop in t,
  })
}

function flatten(menu, trail, out, depth) {
  if (!Array.isArray(menu) || depth > 8) return
  for (const item of menu) {
    if (!item || typeof item !== 'object') continue
    const label = String(item.text == null ? '' : item.text).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()
    const here = trail.concat(label || '?')
    if (item.menu) {
      flatten(item.menu, here, out, depth + 1)
      continue
    }
    if (typeof item.href !== 'string' || !item.href) continue
    out.push({
      path: here,
      text: label || item.href,
      href: item.href,
      input: item.params && typeof item.params === 'object' ? item.params : {},
      default: !!item.default,
      depth,
    })
  }
}

async function main() {
  const appDir = process.argv[2]
  const ctx = process.argv[3] ? JSON.parse(process.argv[3]) : {}
  if (!appDir) { console.log(JSON.stringify({ ok: false, error: 'no app dir' })); return }

  // Athena's launcher is athena.js. An app that predates the rename carries the
  // legacy name instead (plus a shim), and the engine resolves only that one.
  const file = ['athena.js', 'pinokio.js']
    .map((n) => path.join(appDir, n))
    .find((p) => fs.existsSync(p))
  if (!file) {
    console.log(JSON.stringify({ ok: false, error: 'no launcher in this app' })); return
  }

  let mod
  try {
    // Fresh copy every time: menus close over their own module state.
    delete require.cache[require.resolve(file)]
    mod = require(file)
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: `cannot load ${path.basename(file)}: ` + e.message })); return
  }

  const info = makeInfo(appDir, ctx)
  const kernel = makeKernel(info, appDir)

  let menu = mod && mod.menu
  try {
    if (typeof menu === 'function') menu = await menu(kernel, info)
    else menu = menu
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: 'menu() threw: ' + e.message })); return
  }

  const leaves = []
  flatten(menu, [], leaves, 0)

  // An entry the top level marks as the default, if there is one. That is what
  // the engine resolves for `mode:"open"`, and it is absent for apps that insist
  // on a choice.
  const topDefault = (Array.isArray(menu) ? menu : []).find(
    (m) => m && m.default && typeof m.href === 'string' && m.href
  )

  console.log(JSON.stringify({
    ok: true,
    title: (mod && mod.title) || null,
    topDefault: topDefault
      ? { text: String(topDefault.text || ''), href: topDefault.href,
          input: topDefault.params && typeof topDefault.params === 'object' ? topDefault.params : {} }
      : null,
    leaves,
  }))
}

main().catch((e) => {
  console.log(JSON.stringify({ ok: false, error: String(e && e.message || e) }))
})
