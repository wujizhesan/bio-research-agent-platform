import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'

const allowedLicenses = new Set([
  'Apache-2.0',
  'BSD-2-Clause',
  'BSD-3-Clause',
  'BlueOak-1.0.0',
  'CC-BY-4.0',
  'CC0-1.0',
  'ISC',
  'MIT',
  'MIT-0',
  'MPL-2.0',
])

const lockfile = JSON.parse(await readFile(resolve('package-lock.json'), 'utf8'))
if (lockfile.lockfileVersion !== 3 || typeof lockfile.packages !== 'object') {
  throw new Error('package-lock.json must use lockfileVersion 3 with a packages map')
}

const packages = Object.entries(lockfile.packages)
  .filter(([location]) => location !== '')
  .map(([location, metadata]) => ({
    location,
    version: metadata.version ?? '',
    license: metadata.license ?? '',
  }))
  .sort((left, right) => left.location.localeCompare(right.location))

const violations = packages.filter(
  ({ license, version }) => !version || !allowedLicenses.has(license),
)
const report = {
  status: violations.length === 0 ? 'passed' : 'failed',
  counts: {
    packages: packages.length,
    allowed: packages.length - violations.length,
    violations: violations.length,
  },
  allowedLicenses: [...allowedLicenses].sort(),
  violations,
  packages,
}

const outputPath = process.argv[2]
if (outputPath) {
  const resolvedOutput = resolve(outputPath)
  await mkdir(dirname(resolvedOutput), { recursive: true })
  await writeFile(resolvedOutput, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
}

console.log(JSON.stringify({ status: report.status, counts: report.counts }))
if (violations.length > 0) {
  console.error(JSON.stringify(violations, null, 2))
  process.exitCode = 1
}
