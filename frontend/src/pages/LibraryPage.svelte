<script lang="ts">
  import AppHeader from '../components/AppHeader.svelte'
  import CatalogMetrics from '../components/CatalogMetrics.svelte'
  import CatalogToolbar from '../components/CatalogToolbar.svelte'
  import DeleteConfirmDialog from '../components/DeleteConfirmDialog.svelte'
  import ImportDialog from '../components/ImportDialog.svelte'
  import MediaCatalog from '../components/MediaCatalog.svelte'
  import PlaybackRouteDialog from '../components/PlaybackRouteDialog.svelte'
  import { adminApi } from '../lib/admin-api'
  import { filterAndSortMedia, recount } from '../lib/catalog'
  import type { Catalog, LayoutMode, ManualImportResult, Media, MediaTypeFilter, PlaybackRoutes, Policy } from '../lib/types'

  export let credentials: string
  export let initialCatalog: Catalog
  export let onLogout: () => void

  let catalog = initialCatalog
  let pageError = ''
  let syncing = false
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

  $: sortedMedia = filterAndSortMedia(catalog.movies, query, policyFilter, mediaTypeFilter, sortOrder)

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
    syncing = true
    pageError = ''
    try {
      catalog = await adminApi<Catalog>(credentials, '/sync', { method: 'POST' })
      selected = new Set()
    } catch (error) {
      pageError = error instanceof Error ? error.message : '同步失败，请稍后重试'
    } finally {
      syncing = false
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
  <AppHeader {syncing} onSync={syncCatalog} onManualImport={() => (manualOpen = true)} {onLogout} />
  <main class="workspace">
    <CatalogMetrics {catalog} bind:policyFilter />
    <section class="catalog-panel">
      <CatalogToolbar bind:layoutMode bind:mediaTypeFilter bind:sortOrder bind:query resultCount={sortedMedia.length} />
      {#if pageError}<div class="page-error" role="alert">{pageError}</div>{/if}
      <MediaCatalog media={sortedMedia} {layoutMode} {selected} {updating} {bulkBusy} onSelectionChange={(value) => (selected = value)} onUpdatePolicy={updatePolicy} onConfigureRoute={openPlaybackRoutes} onBulkPolicy={bulkPolicy} onDelete={() => (deleteConfirm = true)} onResetFilters={resetFilters} />
    </section>
  </main>
  <DeleteConfirmDialog bind:open={deleteConfirm} count={selected.size} busy={bulkBusy} onDelete={deleteSelected} />
  <ImportDialog bind:open={manualOpen} onImport={importUrl} onCatalogImported={applyImportedCatalog} />
  <PlaybackRouteDialog bind:open={routeOpen} media={routeMedia} routes={routeData} busy={routeBusy} error={routeError} onSelect={selectPlaybackRoute} onRestoreAuto={restoreAutomaticRoute} />
</div>
