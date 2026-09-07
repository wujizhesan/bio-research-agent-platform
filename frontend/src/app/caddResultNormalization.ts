import type { CaddHit } from './types'
import { recordArray, stringValue, type ResultRecord } from './resultNormalizationShared'

export type CaddResultViewModel = {
  raw: ResultRecord
  hits: CaddHit[]
  maxAbsAffinity: number
  scorePlot: string
  topMoleculeImage: string
  bestHit: string
  bestAffinity?: number
  rows: unknown
  maxLigands: unknown
  hasData: boolean
}

export function normalizeCaddHits(result: ResultRecord): CaddHit[] {
  return recordArray(result.hits ?? result.top_hits).flatMap((item) => {
    const affinity = Number(item.affinity)
    if (!Number.isFinite(affinity)) return []
    return [{
      mol_name: String(item.mol_name || item.name || 'unknown'),
      tag: String(item.tag || 'inactive'),
      affinity,
    }]
  })
}

export function normalizeCaddResult(result: ResultRecord): CaddResultViewModel {
  const hits = normalizeCaddHits(result)
  return {
    raw: result,
    hits,
    maxAbsAffinity: Math.max(...hits.map((hit) => Math.abs(hit.affinity)), 1),
    scorePlot: stringValue(result.score_plot) ?? '',
    topMoleculeImage: stringValue(result.top_molecule_image) ?? '',
    bestHit: String(result.best_hit || hits[0]?.mol_name || '--'),
    bestAffinity: result.best_affinity !== undefined ? Number(result.best_affinity) : hits[0]?.affinity,
    rows: result.rows ?? hits.length,
    maxLigands: result.max_ligands ?? (hits.length || '--'),
    hasData: Boolean(result.best_hit) || Array.isArray(result.hits) || Array.isArray(result.top_hits),
  }
}
