export type ResultRecord = Record<string, unknown>

export function isRecord(value: unknown): value is ResultRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

export function recordValue(value: unknown): ResultRecord {
  return isRecord(value) ? value : {}
}

export function recordArray(value: unknown): ResultRecord[] {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

export function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

export function stringValue(value: unknown) {
  return typeof value === 'string' ? value : undefined
}

export function metricNumber(metrics: ResultRecord, keys: string[]) {
  for (const key of keys) {
    const value = Number(metrics[key])
    if (Number.isFinite(value)) return value
  }
  return undefined
}

export function percentMetric(metrics: ResultRecord, keys: string[]) {
  const value = metricNumber(metrics, keys)
  if (value === undefined) return undefined
  return value <= 1 ? value * 100 : value
}
