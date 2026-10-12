import { describe, expect, it } from 'vitest'
import { filterAndSortMedia, recount } from './catalog'
import type { Media } from './types'

function media(overrides: Partial<Media>): Media {
  return {
    xlys_id: 1,
    provider: 'xlys',
    source_media_id: '1',
    title: '同名电影',
    year: 2026,
    cover_url: null,
    douban_rating: null,
    source_updated_on: '2026-10-01',
    policy: 'auto',
    dav_filename: '同名电影 (2026).strm',
    play_page_url: 'https://www.xlys02.com/play/1-0.htm',
    kind: 'movie',
    available_episode_count: 0,
    ...overrides,
  }
}

describe('catalog filtering', () => {
  const items = [
    media({ xlys_id: 1, policy: 'keep' }),
    media({ xlys_id: -1, provider: 'libvio', policy: 'keep' }),
    media({ xlys_id: -2, provider: 'libvio', kind: 'series', policy: 'keep' }),
    media({ xlys_id: -3, provider: 'libvio', policy: 'hidden' }),
    media({ xlys_id: -4, provider: 'libvio', policy: 'keep', year: 2025 }),
  ]

  it('combines all filters even when providers share a source ID and title', () => {
    const result = filterAndSortMedia(items, ' 2026 ', 'keep', 'movie', 'updated-desc', 'libvio')
    expect(result.map((item) => item.xlys_id)).toEqual([-1])
    expect(filterAndSortMedia(items, '同名', 'keep', 'movie', 'updated-desc', 'xlys')).toEqual([
      items[0],
    ])
  })

  it('keeps hidden titles available to management and supports the all-provider default', () => {
    expect(filterAndSortMedia(items, '', 'hidden', 'all', 'title-asc', 'libvio')).toEqual([
      items[3],
    ])
    expect(filterAndSortMedia(items, '', 'all', 'all', 'title-asc')).toHaveLength(5)
    expect(filterAndSortMedia(items, 'missing', 'all', 'all', 'title-asc', 'libvio')).toEqual([])
  })

  it('sorts mixed-provider results without changing the original catalog order', () => {
    const source = [
      media({ xlys_id: 1, title: 'Episode 10', source_updated_on: null }),
      media({ xlys_id: -1, provider: 'libvio', title: 'episode 2' }),
      media({ xlys_id: -2, provider: 'libvio', title: 'Episode 1' }),
    ]
    expect(
      filterAndSortMedia(source, 'EPISODE', 'all', 'all', 'title-asc').map((item) => item.xlys_id),
    ).toEqual([-2, -1, 1])
    expect(filterAndSortMedia(source, '', 'all', 'all', 'updated-desc').at(-1)?.xlys_id).toBe(1)
    expect(source.map((item) => item.xlys_id)).toEqual([1, -1, -2])
  })
})

describe('catalog counts', () => {
  it('recounts after changing policy or removing a title across suppliers', () => {
    const items = [
      media({ policy: 'keep' }),
      media({ xlys_id: -1, provider: 'libvio', kind: 'series' }),
      media({ xlys_id: -2, provider: 'libvio', policy: 'hidden' }),
    ]
    const updated = items
      .filter((item) => item.xlys_id !== 1)
      .map((item) => ({ ...item, policy: 'keep' as const }))
    expect(recount(updated)).toEqual({
      total: 2,
      automatic: 0,
      kept: 2,
      hidden: 0,
      movies: 1,
      series: 1,
    })
    expect(recount(items)).toEqual({
      total: 3,
      automatic: 1,
      kept: 1,
      hidden: 1,
      movies: 2,
      series: 1,
    })
  })
})
