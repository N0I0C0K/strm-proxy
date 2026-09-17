export type Policy = 'auto' | 'keep' | 'hidden'
export type MediaKind = 'movie' | 'series'
export type MediaTypeFilter = 'all' | MediaKind
export type LayoutMode = 'list' | 'grid'

export type Media = {
  xlys_id: number
  title: string
  year: number | null
  cover_url: string | null
  douban_rating: number | null
  source_updated_on: string | null
  policy: Policy
  dav_filename: string
  play_page_url: string
  kind: MediaKind
  available_episode_count: number
}

export type MediaDetail = Media & {
  source_url: string | null
  declared_episode_count: number | null
  season_number: number | null
  last_checked_at: string | null
  last_watched_at: string | null
  episodes: { source_index: number; label: string; play_page_url: string }[]
}

export type CatalogCounts = {
  total: number
  automatic: number
  kept: number
  hidden: number
  movies: number
  series: number
}

export type Catalog = {
  movies: Media[]
  counts: CatalogCounts
  recent_limit: number
  recently_watched_series_count: number
}

export type RefreshItem = {
  xlys_id: number
  title: string
  kind: MediaKind
  added_episodes: number
  available_episode_count: number
}

export type RefreshResult = { item: RefreshItem; catalog: Catalog }

export type RecentRefreshResult = {
  checked: number
  refreshed: number
  added_episodes: number
  failures: { xlys_id: number; title: string; error: string }[]
  catalog: Catalog
}

export type ManualImportResult = {
  kind: MediaKind
  imported: boolean
  xlys_id: number
  title: string
  year: number | null
  cover_url: string | null
  available_episode_count: number
  declared_episode_count: number | null
  source_url: string
  message: string
  catalog: Catalog | null
}

export type PlaybackRouteOption = {
  line: number
  name: string | null
  kind: string
  selectable: boolean
}

export type PlaybackRoutes = {
  page_url: string
  title: string | null
  media_kind: MediaKind
  cache_enabled: boolean
  cached_source: 'hls' | 'tos' | 'member' | null
  manual_override: boolean
  selected_line: number | null
  selected_route_name: string | null
  routes: PlaybackRouteOption[]
}

export const policyMeta: Record<Policy, { label: string; short: string }> = {
  auto: { label: '跟随自动更新', short: '自动' },
  keep: { label: '始终出现在片库', short: '保留' },
  hidden: { label: '不在片库显示', short: '隐藏' },
}

export const policyOptions = Object.entries(policyMeta).map(([value, meta]) => ({
  value,
  label: meta.short,
}))

export const sortOptions = [
  { value: 'updated-desc', label: '更新时间：最新' },
  { value: 'updated-asc', label: '更新时间：最早' },
  { value: 'title-asc', label: '名称：A–Z' },
  { value: 'title-desc', label: '名称：Z–A' },
]

export const mediaTypeOptions = [
  { value: 'all', label: '全部类型' },
  { value: 'movie', label: '电影' },
  { value: 'series', label: '电视剧' },
]
