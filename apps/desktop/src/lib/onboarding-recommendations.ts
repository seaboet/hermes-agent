import type { McpCatalogAvailability, McpCatalogEntry } from '@/types/hermes'

export interface OnboardingInterests {
  apps?: readonly string[]
  context?: string
}

export interface OnboardingRecommendation {
  name: string
  description: string
  examples: string[]
  availability: McpCatalogAvailability
  readiness: 'configured_unverified' | 'setup_required'
  requiresApp: boolean
  authType: string
  setupAction: 'install' | 'enable' | null
}

const words = (text: string): string => text.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean).join(' ')

/** The backend saw the application this entry needs (present, version acceptable). */
const appDetected = (entry: McpCatalogEntry): boolean => entry.availability.state === 'available'

/** Rank evidence, not a fixed list of products. The model derives outcomes from catalog descriptions; curated examples are optional. */
export function onboardingRecommendations(
  entries: readonly McpCatalogEntry[],
  { apps = [], context = '' }: OnboardingInterests = {}
): OnboardingRecommendation[] {
  const selected = new Set(apps.map(words))
  const subject = ` ${words(context)} `

  const candidates = entries.flatMap(entry => {
    const examples = [...new Set(entry.suggest?.examples ?? [])].filter(text => text.trim())

    const terms = [entry.name, ...(entry.suggest?.keywords ?? []), ...(entry.suggest?.applications ?? [])].map(words).filter(Boolean)
    const preferred = terms.some(term => selected.has(term))
    const topical = terms.some(term => subject.includes(` ${term} `))
    const detected = appDetected(entry)
    const configured = entry.installed && entry.enabled

    // An explicit task can request a disabled integration; mere discovery must not undo that choice.
    if (entry.installed && !entry.enabled && !preferred && !topical) {
      return []
    }

    // The app this MCP fronts is absent, too old, or this OS is unsupported: nothing to recommend,
    // even when the MCP itself is configured (the user may have uninstalled the app since).
    if (entry.requires_app && !detected) {
      return []
    }

    if (!configured && !detected && !preferred && !topical) {
      return []
    }

    const recommendation: OnboardingRecommendation = {
      name: entry.name,
      description: entry.description,
      examples: examples.slice(0, preferred || topical ? 3 : 1),
      availability: entry.availability,
      readiness: configured ? 'configured_unverified' : 'setup_required',
      requiresApp: entry.requires_app,
      authType: entry.auth_type,
      setupAction: !entry.installed ? 'install' : !entry.enabled ? 'enable' : null
    }

    return [{ recommendation, topical, preferred, configured, detected }]
  })

  return candidates.sort((a, b) =>
    Number(b.topical) - Number(a.topical)
    || Number(b.preferred) - Number(a.preferred)
    || Number(b.configured) - Number(a.configured)
    || Number(b.detected) - Number(a.detected)
    || a.recommendation.name.localeCompare(b.recommendation.name)
  ).slice(0, 6).map(candidate => candidate.recommendation)
}
