/**
 * Render the network block for real, without a browser.
 *
 * The merged plugin.js is an ES module full of plugin-sdk imports, so it cannot
 * be imported here. The four functions the network block is made of are lifted
 * out of the file and called with a stub jsx/jsxs that builds plain objects --
 * which is enough to walk the tree, read the numbers that would be on screen,
 * and catch the things a syntax check cannot: a missing guard on an absent
 * payload, a divide by zero, a sparkline that draws when it has no samples.
 *
 * Usage: node probe_net_ui.mjs <path to merged plugin.js>
 */
import { readFileSync } from 'node:fs'

const file = process.argv[2]
const src = readFileSync(file, 'utf8')

/** The source of one `function name(...) { ... }`, braces balanced. */
function fn(name) {
  const i = src.indexOf(`function ${name}(`)
  if (i === -1) throw new Error(`not found in the merged plugin: function ${name}`)
  // Skip the parameter list first: a destructured signature like `{ net }` would
  // otherwise look like the function body and the slice would end there.
  let paren = 0, j = src.indexOf('(', i)
  for (let k = j; k < src.length; k++) {
    if (src[k] === '(') paren++
    else if (src[k] === ')') { paren--; if (paren === 0) { j = k + 1; break } }
  }
  let depth = 0
  for (let k = j; k < src.length; k++) {
    const c = src[k]
    if (c === '{') depth++
    else if (c === '}') {
      depth--
      if (depth === 0) return src.slice(i, k + 1)
    }
  }
  throw new Error(`unbalanced: ${name}`)
}

const jsx = (type, props) => ({ type, props: props || {} })
const jsxs = jsx
const source = ['fmtBps', 'fmtMB', 'NetSpark', 'NetPanel'].map(fn).join('\n')
const build = new Function('jsx', 'jsxs', `${source}; return { fmtBps, fmtMB, NetSpark, NetPanel }`)
const { fmtBps, fmtMB, NetPanel, NetSpark } = build(jsx, jsxs)

let passed = 0, failed = 0
const check = (label, ok, detail = '') => {
  if (ok) { passed++; console.log(`  ok   ${label}`) }
  else { failed++; console.log(`  FAIL ${label}   ${detail}`) }
}

/** Every string a rendered tree would put on screen. */
function text(node, out = []) {
  if (node == null || node === false) return out
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (Array.isArray(node)) { node.forEach((n) => text(n, out)); return out }
  if (typeof node === 'object') text(node.props?.children, out)
  return out
}

/** Every rendered element of a given type. */
function find(node, type, out = []) {
  if (node == null || typeof node !== 'object') return out
  if (Array.isArray(node)) { node.forEach((n) => find(n, type, out)); return out }
  if (node.type === type) out.push(node)
  find(node.props?.children, type, out)
  return out
}

const payload = {
  up_bps: 20804,
  down_bps: 25740906,
  sent_mb: 63965.0,
  recv_mb: 824125.6,
  speed_mbps: 864,
  peak_down_bps: 30000000,
  peak_up_bps: 900000,
  history: [[0, 0], [12000000, 400000], [25740906, 20804], [9000000, 0], [1000, 500]],
  nics: [
    { name: 'Wi-Fi', up_bps: 20804, down_bps: 25740906, sent_mb: 63938.9, recv_mb: 824126.0, speed_mbps: 864 },
    { name: 'Tailscale', up_bps: 0, down_bps: 0, sent_mb: 13.1, recv_mb: 0.1, speed_mbps: 4294 },
  ],
}

console.log('\n1. the numbers a person would read off the panel')
{
  const tree = NetPanel({ net: payload })
  const shown = text(tree).join(' | ')
  check('the adapter it is actually on is named', shown.includes('Network · Wi-Fi'), shown.slice(0, 160))
  check('the down rate reads as a rate', shown.includes('↓') && shown.includes('24.5 MB/s'), shown)
  check('the up rate likewise', shown.includes('20 KB/s'), shown)
  check('the link speed is the busiest adapter\'s, not the fastest nominal',
        shown.includes('864 Mb/s'), shown)
  check('the total since boot is there, in GB', shown.includes('804.8 GB'), shown)
  check('and so is what went out', shown.includes('62.5 GB'), shown)
  check('a peak puts the current rate in context', shown.includes('peak ↓ 28.6 MB/s'), shown)
  check('the second adapter is listed when there is one', shown.includes('Tailscale'), shown)
}

console.log('\n2. the bar is a share of the link, not of nothing')
{
  const track = find(NetPanel({ net: payload }), 'div')
    .find((e) => e.props.className === 'hermes-sysmon-track')
  check('a link speed means a bar exists', !!track)
  const fill = find(track, 'div').find((e) => e.props.className === 'hermes-sysmon-fill')
  const width = parseFloat(fill.props.style.width)
  // 25.7 MB/s down + 20 KB/s up over 864 Mb/s = 23.8% of the link.
  check('the width is bytes-to-megabits against the link speed',
        Math.abs(width - 23.8) < 0.5, width + '%')
  check('a saturated link marks itself rather than only filling',
        find(NetPanel({ net: { ...payload, down_bps: 108000000 } }), 'div')
          .some((e) => e.props.className === 'hermes-sysmon-fill' && e.props['data-hot'] === true))
  check('no link speed reported means no bar, not a 0% one that looks measured',
        !find(NetPanel({ net: { ...payload, speed_mbps: 0 } }), 'div')
          .some((e) => e.props.className === 'hermes-sysmon-track'))
}

console.log('\n3. the sparkline draws the history, and only when there is one')
{
  const svg = NetSpark({ history: payload.history })
  check('it draws with samples', svg && svg.type === 'svg')
  const lines = find(svg, 'polyline')
  check('two lines: down and up', lines.length === 2, lines.length)
  check('one point per sample', lines[0].props.points.split(' ').length === 5,
        lines[0].props.points)
  check('the same scale for both directions, so an upload cannot look like a download',
        Math.max(...lines[1].props.points.split(' ').map((p) => parseFloat(p.split(',')[1]))) <
        Math.max(...lines[0].props.points.split(' ').map((p) => parseFloat(p.split(',')[1]))) + 30)
  check('the top of the trace is the peak sample, keeping it inside the box',
        Math.min(...lines.map((l) => Math.min(...l.props.points.split(' ')
          .map((p) => parseFloat(p.split(',')[1]))))) >= 0)
  check('too few samples draws nothing rather than a flat lie',
        NetSpark({ history: [[1, 2]] }) === null)
  check('no history at all is not a crash', NetSpark({}) === null)
}

console.log('\n4. a payload that is missing everything is not a crash')
{
  const empty = NetPanel({ net: undefined })
  check('it renders anyway', !!empty)
  const shown = text(empty).join(' | ')
  check('with a dash where the link speed would be', shown.includes('—'), shown)
  check('and no adapter name invented', !shown.includes('·'), shown)
  check('and no NaN or undefined leaking into the screen',
        !/NaN|undefined/.test(shown), shown)
  check('a payload with no nics array at all is equally safe',
        !/NaN|undefined/.test(text(NetPanel({ net: { down_bps: 5 } })).join(' | ')))
}

console.log('\n5. the formatters')
{
  check('bytes per second', fmtBps(25740906) === '24.5 MB/s', fmtBps(25740906))
  check('zero is zero', fmtBps(0) === '0 B/s', fmtBps(0))
  check('MB totals', fmtMB(63965) === '62.5 GB', fmtMB(63965))
  check('small totals in KB, not 0 GB', fmtMB(0.4) === '410 KB', fmtMB(0.4))
}

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
