import { readFile, readdir, stat } from 'node:fs/promises'
import { gzipSync } from 'node:zlib'
import { extname, resolve, sep } from 'node:path'

const kilobyte = 1000
const distDirectory = resolve(process.cwd(), 'dist')

function budgetFromEnvironment(name, fallbackKB) {
  const rawValue = process.env[name]
  if (rawValue === undefined) return fallbackKB * kilobyte
  const value = Number(rawValue)
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} 必须是大于 0 的 kB 数值`)
  return value * kilobyte
}

const budgets = {
  initialJavaScriptGzip: budgetFromEnvironment('PERF_BUDGET_JS_GZIP_KB', 100),
  initialCssGzip: budgetFromEnvironment('PERF_BUDGET_CSS_GZIP_KB', 15),
  totalBuild: budgetFromEnvironment('PERF_BUDGET_TOTAL_KB', 400),
}

async function listFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const nested = await Promise.all(entries.map((entry) => {
    const path = resolve(directory, entry.name)
    return entry.isDirectory() ? listFiles(path) : [path]
  }))
  return nested.flat()
}

function initialAssetPaths(html) {
  const references = [...html.matchAll(/(?:src|href)=["']([^"']+\.(?:js|css)(?:[?#][^"']*)?)["']/gi)]
    .map((match) => match[1])
    .filter((reference) => !/^(?:https?:)?\/\//i.test(reference))
    .map((reference) => reference.split(/[?#]/, 1)[0].replace(/^\/+/, ''))
    .map((reference) => resolve(distDirectory, reference))
  const outsideBuild = references.find((path) => path !== distDirectory && !path.startsWith(`${distDirectory}${sep}`))
  if (outsideBuild) throw new Error(`首屏资源路径越过 dist 目录：${outsideBuild}`)
  return [...new Set(references)]
}

function formatKB(bytes) {
  return `${(bytes / kilobyte).toFixed(2)} kB`
}

const html = await readFile(resolve(distDirectory, 'index.html'), 'utf8')
const initialAssets = initialAssetPaths(html)
if (!initialAssets.some((path) => extname(path) === '.js')) throw new Error('未在 dist/index.html 中找到首屏 JavaScript')

const initialAssetContents = await Promise.all(initialAssets.map((path) => readFile(path)))
const initialJavaScriptGzip = initialAssets.reduce((total, path, index) => (
  extname(path) === '.js' ? total + gzipSync(initialAssetContents[index]).byteLength : total
), 0)
const initialCssGzip = initialAssets.reduce((total, path, index) => (
  extname(path) === '.css' ? total + gzipSync(initialAssetContents[index]).byteLength : total
), 0)
const buildFiles = await listFiles(distDirectory)
const buildStats = await Promise.all(buildFiles.map((path) => stat(path)))
const totalBuild = buildStats.reduce((total, file) => total + file.size, 0)

const measurements = [
  ['首屏 JavaScript gzip', initialJavaScriptGzip, budgets.initialJavaScriptGzip],
  ['首屏 CSS gzip', initialCssGzip, budgets.initialCssGzip],
  ['完整构建体积', totalBuild, budgets.totalBuild],
]

console.log('前端性能预算')
for (const [label, actual, budget] of measurements) {
  const utilization = ((actual / budget) * 100).toFixed(1)
  console.log(`${label}: ${formatKB(actual)} / ${formatKB(budget)} (${utilization}%)`)
}

const violations = measurements.filter(([, actual, budget]) => actual > budget)
if (violations.length) {
  for (const [label, actual, budget] of violations) {
    console.error(`${label} 超出预算 ${formatKB(actual - budget)}`)
  }
  process.exitCode = 1
}
