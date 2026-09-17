<script lang="ts">
  import AppHeader from '../components/AppHeader.svelte'
  import CatalogMetrics from '../components/CatalogMetrics.svelte'
  import CatalogPagination from '../components/CatalogPagination.svelte'
  import CatalogToolbar from '../components/CatalogToolbar.svelte'
  import DeleteConfirmDialog from '../components/DeleteConfirmDialog.svelte'
  import ImportDialog from '../components/ImportDialog.svelte'
  import MediaCatalog from '../components/MediaCatalog.svelte'
  import MediaDetailDialog from '../components/MediaDetailDialog.svelte'
  import PlaybackRouteDialog from '../components/PlaybackRouteDialog.svelte'
  import { adminApi } from '../lib/admin-api'
  import { filterAndSortMedia, recount } from '../lib/catalog'
  import type { Catalog, LayoutMode, ManualImportResult, Media, MediaDetail, MediaTypeFilter, PlaybackRoutes, Policy, RefreshResult, RecentRefreshResult } from '../lib/types'

  export let credentials: string
  export let initialCatalog: Catalog
  export let onLogout: () => void

  let catalog = initialCatalog
  let pageError = ''
  let pageNotice = ''
  let syncing = false
  let recentRefreshing = false
  let refreshing = new Set<number>()
  let updating = new Set<number>()
  let selected = new Set<number>()
  let bulkBusy = false
  let deleteConfirm = false
  let manualOpen = false
  let routeOpen = false
  let routeMedia: Media | null = null
  let routeData: PlaybackRoutes | null = null
  let routeBusy = false
  let routeError = ''
  let query = ''
  let policyFilter: 'all' | Policy = 'all'
  let mediaTypeFilter: MediaTypeFilter = 'all'
  let sortOrder = 'updated-desc'
  let layoutMode: LayoutMode = 'list'
  const pageSize = 24
  let page = 1
  let lastFilterSignature = ''
  let detailOpen = false
  let detailMediaId: number | null = null
  let detailData: MediaDetail | null = null
  let detailLoading = false
  let detailActionBusy = false
  let detailError = ''
  let detailNotice = ''

  $: sortedMedia = filterAndSortMedia(catalog.movies, query, policyFilter, mediaTypeFilter, sortOrder)
  $: {
    const signature = JSON.stringify([query, policyFilter, mediaTypeFilter, sortOrder, layoutMode])
    if (signature !== lastFilterSignature) {
      page = 1
      lastFilterSignature = signature
    }
  }
  $: pageCount = Math.max(1, Math.ceil(sortedMedia.length / pageSize))
  $: if (page > pageCount) page = pageCount
  $: visibleMedia = sortedMedia.slice((page - 1) * pageSize, page * pageSize)
  $: activeDetailMedia = catalog.movies.find((item) => item.xlys_id === detailMediaId) ?? null

  function changePage(next: number) {
    page = Math.max(1, Math.min(next, pageCount))
    document.querySelector('.catalog-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  async function loadDetail(xlysId: number) {
    detailLoading = true
    try {
      const result = await adminApi<MediaDetail>(credentials, `/media/${xlysId}`)
      if (detailMediaId === xlysId && detailOpen) detailData = result
    } catch (error) {
      if (detailMediaId === xlysId && detailOpen) detailError = error instanceof Error ? error.message : '读取影片详情失败'
    } finally {
      if (detailMediaId === xlysId) detailLoading = false
    }
  }

  async function openDetail(media: Media) {
    detailMediaId = media.xlys_id
    detailData = null
    detailError = ''
    detailNotice = ''
    detailOpen = true
    await loadDetail(media.xlys_id)
  }

  async function refreshDetail() {
    if (!activeDetailMedia || detailActionBusy || detailLoading || syncing || recentRefreshing || refreshing.size) return
    const media = activeDetailMedia
    detailActionBusy = true
    detailError = ''
    detailNotice = ''
    try {
      const result = await adminApi<RefreshResult>(credentials, `/media/${media.xlys_id}/refresh`, { method: 'POST' })
      catalog = result.catalog
      await loadDetail(media.xlys_id)
      detailNotice = result.item.kind === 'series'
        ? `刷新完成，新增 ${result.item.added_episodes} 集。`
        : '刷新完成。'
    } catch (error) {
      detailError = error instanceof Error ? error.message : '刷新影片失败'
    } finally {
      detailActionBusy = false
    }
  }

  async function updateDetailPolicy(policy: Policy) {
    if (!activeDetailMedia || detailActionBusy || activeDetailMedia.policy === policy) return
    detailActionBusy = true
    detailError = ''
    detailNotice = ''
    try {
      const updated = await adminApi<Media>(credentials, `/media/${activeDetailMedia.xlys_id}/policy`, {
        method: 'PATCH',
        body: JSON.stringify({ policy }),
      })
      const movies = catalog.movies.map((item) => item.xlys_id === updated.xlys_id ? updated : item)
      catalog = { ...catalog, movies, counts: recount(movies) }
      if (detailData?.xlys_id === updated.xlys_id) detailData = { ...detailData, policy }
      detailNotice = '片库策略已更新。'
    } catch (error) {
      detailError = error instanceof Error ? error.message : '更新片库策略失败'
    } finally {
      detailActionBusy = false
    }
  }

  function configureDetailRoute() {
    if (!activeDetailMedia) return
    const media = activeDetailMedia
    detailOpen = false
    openPlaybackRoutes(media)
  }

  async function updatePolicy(media: Media, policy: Policy) {
    if (media.policy === policy || updating.has(media.xlys_id)) return
    updating = new Set(updating).add(media.xlys_id)
    pageError = ''
    try {
      const updated = await adminApi<Media>(credentials, `/media/${media.xlys_id}/policy`, {
        method: 'PATCH',
        body: JSON.stringify({ policy }),
      })
      const movies = catalog.movies.map((item) => item.xlys_id === updated.xlys_id ? updated : item)
      catalog = { ...catalog, movies, counts: recount(movies) }
    } catch (error) {
      pageError = error instanceof Error ? error.message : '更新影片策略失败'
    } finally {
      updating.delete(media.xlys_id)
      updating = new Set(updating)
    }
  }

  async function syncCatalog() {
    if (syncing || recentRefreshing || refreshing.size) return
    syncing = true
    pageError = ''
    pageNotice = ''
    try {
      catalog = await adminApi<Catalog>(credentials, '/sync', { method: 'POST' })
      selected = new Set()
    } catch (error) {
      pageError = error instanceof Error ? error.message : '同步失败，请稍后重试'
    } finally {
      syncing = false
    }
  }

  async function refreshMedia(media: Media) {
    if (syncing || recentRefreshing || refreshing.has(media.xlys_id)) return
    refreshing = new Set(refreshing).add(media.xlys_id)
    pageError = ''
    pageNotice = ''
    try {
      const result = await adminApi<RefreshResult>(credentials, `/media/${media.xlys_id}/refresh`, { method: 'POST' })
      catalog = result.catalog
      pageNotice = result.item.kind === 'series'
        ? `《${result.item.title}》已刷新，新增 ${result.item.added_episodes} 集，当前共 ${result.item.available_episode_count} 集。`
        : `《${result.item.title}》已刷新。`
    } catch (error) {
      pageError = `刷新《${media.title}》失败：${error instanceof Error ? error.message : '请稍后重试'}`
    } finally {
      refreshing.delete(media.xlys_id)
      refreshing = new Set(refreshing)
    }
  }

  async function refreshRecentSeries() {
    if (syncing || recentRefreshing || refreshing.size || catalog.recently_watched_series_count === 0) return
    recentRefreshing = true
    pageError = ''
    pageNotice = ''
    try {
      const result = await adminApi<RecentRefreshResult>(credentials, '/media/refresh-recent-series', { method: 'POST' })
      catalog = result.catalog
      pageNotice = `最近观看的电视剧：检查 ${result.checked} 部，成功 ${result.refreshed} 部，新增 ${result.added_episodes} 集。`
      if (result.failures.length) {
        pageError = `刷新失败 ${result.failures.length} 部：${result.failures.map((item) => `《${item.title}》${item.error}`).join('；')}`
      }
    } catch (error) {
      pageError = error instanceof Error ? error.message : '刷新最近观看的电视剧失败'
    } finally {
      recentRefreshing = false
    }
  }

  async function bulkPolicy(policy: Policy) {
    if (!selected.size || bulkBusy) return
    bulkBusy = true
    pageError = ''
    try {
      catalog = await adminApi<Catalog>(credentials, '/media/batch/policy', {
        method: 'PATCH',
        body: JSON.stringify({ xlys_ids: [...selected], policy }),
      })
      selected = new Set()
    } catch (error) {
      pageError = error instanceof Error ? error.message : '批量更新失败'
    } finally {
      bulkBusy = false
    }
  }

  async function deleteSelected() {
    if (!selected.size || bulkBusy) return
    bulkBusy = true
    pageError = ''
    try {
      catalog = await adminApi<Catalog>(credentials, '/media/batch', {
        method: 'DELETE',
        body: JSON.stringify({ xlys_ids: [...selected] }),
      })
      selected = new Set()
      deleteConfirm = false
    } catch (error) {
      pageError = error instanceof Error ? error.message : '批量删除失败'
    } finally {
      bulkBusy = false
    }
  }

  async function importUrl(url: string): Promise<ManualImportResult> {
    return adminApi<ManualImportResult>(credentials, '/import', {
      method: 'POST',
      body: JSON.stringify({ url }),
    })
  }

  function applyImportedCatalog(result: ManualImportResult) {
    if (!result.catalog) return
    catalog = result.catalog
    selected = new Set()
  }

  async function openPlaybackRoutes(media: Media) {
    routeMedia = media
    routeData = null
    routeError = ''
    routeOpen = true
    routeBusy = true
    try {
      routeData = await adminApi<PlaybackRoutes>(credentials, `/media/${media.xlys_id}/playback-routes`)
    } catch (error) {
      routeError = error instanceof Error ? error.message : '读取播放线路失败'
    } finally {
      routeBusy = false
    }
  }

  async function selectPlaybackRoute(routeName: string) {
    if (!routeMedia || routeBusy) return
    routeBusy = true
    routeError = ''
    try {
      routeData = await adminApi<PlaybackRoutes>(credentials, `/media/${routeMedia.xlys_id}/playback-routes`, {
        method: 'PUT',
        body: JSON.stringify({ route_name: routeName }),
      })
    } catch (error) {
      routeError = error instanceof Error ? error.message : '保存播放线路失败'
    } finally {
      routeBusy = false
    }
  }

  async function restoreAutomaticRoute() {
    if (!routeMedia || routeBusy) return
    routeBusy = true
    routeError = ''
    try {
      await adminApi<{ cleared: true; page_url: string }>(credentials, `/media/${routeMedia.xlys_id}/playback-routes`, {
        method: 'DELETE',
      })
      if (routeData) {
        routeData = {
          ...routeData,
          cached_source: null,
          manual_override: false,
          selected_line: null,
          selected_route_name: null,
        }
      }
    } catch (error) {
      routeError = error instanceof Error ? error.message : '恢复自动选择失败'
    } finally {
      routeBusy = false
    }
  }

  function resetFilters() {
    query = ''
    policyFilter = 'all'
    mediaTypeFilter = 'all'
  }
</script>

<div class="app-shell">
  <AppHeader {syncing} {recentRefreshing} itemRefreshing={refreshing.size > 0} recentSeriesCount={catalog.recently_watched_series_count} onSync={syncCatalog} onRefreshRecent={refreshRecentSeries} onManualImport={() => (manualOpen = true)} {onLogout} />
  <main class="workspace">
    <CatalogMetrics {catalog} bind:policyFilter />
    <section class="catalog-panel">
      <CatalogToolbar bind:layoutMode bind:mediaTypeFilter bind:sortOrder bind:query resultCount={sortedMedia.length} />
      {#if pageError}<div class="page-error" role="alert">{pageError}</div>{/if}
      {#if pageNotice}<div class="page-notice" role="status">{pageNotice}</div>{/if}
      <MediaCatalog media={visibleMedia} {layoutMode} {selected} {updating} {refreshing} refreshDisabled={syncing || recentRefreshing} {bulkBusy} onSelectionChange={(value) => (selected = value)} onUpdatePolicy={updatePolicy} onRefresh={refreshMedia} onOpenDetail={openDetail} onConfigureRoute={openPlaybackRoutes} onBulkPolicy={bulkPolicy} onDelete={() => (deleteConfirm = true)} onResetFilters={resetFilters} />
      <CatalogPagination {page} totalPages={pageCount} totalItems={sortedMedia.length} {pageSize} onPageChange={changePage} />
    </section>
  </main>
  <DeleteConfirmDialog bind:open={deleteConfirm} count={selected.size} busy={bulkBusy} onDelete={deleteSelected} />
  <ImportDialog bind:open={manualOpen} onImport={importUrl} onCatalogImported={applyImportedCatalog} />
  <PlaybackRouteDialog bind:open={routeOpen} media={routeMedia} routes={routeData} busy={routeBusy} error={routeError} onSelect={selectPlaybackRoute} onRestoreAuto={restoreAutomaticRoute} />
  <MediaDetailDialog bind:open={detailOpen} media={activeDetailMedia} detail={detailData} loading={detailLoading} busy={detailActionBusy || syncing || recentRefreshing || refreshing.size > 0} error={detailError} notice={detailNotice} onRefresh={refreshDetail} onPolicyChange={updateDetailPolicy} onConfigureRoute={configureDetailRoute} />
</div>
