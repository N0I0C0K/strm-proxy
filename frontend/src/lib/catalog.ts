import type { CatalogCounts, Media, MediaTypeFilter, Policy } from './types'

export function filterAndSortMedia(
  media: Media[],
  query: string,
  policyFilter: 'all' | Policy,
  mediaTypeFilter: MediaTypeFilter,
  sortOrder: string,
): Media[] {
  const normalizedQuery = query.trim().toLocaleLowerCase()
  return media
    .filter((item) => {
      const matchesPolicy = policyFilter === 'all' || item.policy === policyFilter
      const matchesType = mediaTypeFilter === 'all' || item.kind === mediaTypeFilter
      const matchesQuery =
        !normalizedQuery ||
        item.title.toLocaleLowerCase().includes(normalizedQuery) ||
        String(item.year ?? '').includes(normalizedQuery)
      return matchesPolicy && matchesType && matchesQuery
    })
    .sort((left, right) => {
      const direction = sortOrder.endsWith('asc') ? 1 : -1
      if (sortOrder.startsWith('title')) {
        return left.title.localeCompare(right.title, 'zh-CN', { numeric: true }) * direction
      }
      return (left.source_updated_on ?? '').localeCompare(right.source_updated_on ?? '') * direction
    })
}

export function recount(media: Media[]): CatalogCounts {
  return {
    total: media.length,
    automatic: media.filter((item) => item.policy === 'auto').length,
    kept: media.filter((item) => item.policy === 'keep').length,
    hidden: media.filter((item) => item.policy === 'hidden').length,
    movies: media.filter((item) => item.kind === 'movie').length,
    series: media.filter((item) => item.kind === 'series').length,
  }
}
