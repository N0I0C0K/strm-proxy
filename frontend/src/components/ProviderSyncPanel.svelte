<script lang="ts">
  import { Button } from 'bits-ui'
  import { RefreshCw } from 'lucide-svelte'
  import type { Catalog, Provider, ProviderSync } from '../lib/types'

  export let catalog: Catalog
  export let syncingProviders: Set<Provider>
  export let disabled = false
  export let onSync: (provider?: Provider) => void

  const names = { libvio: 'LIBVIO', xlys: '雪落影视（XLYS）' }
  const labels = {
    idle: '暂无同步记录',
    syncing: '正在同步',
    success: '上次同步成功',
    partial: '上次部分完成',
    failed: '上次同步失败',
  }
  const initial: ProviderSync[] = [
    { provider: 'libvio', status: 'idle', last_synced_at: null, failures: [] },
    { provider: 'xlys', status: 'idle', last_synced_at: null, failures: [] },
  ]
  $: providers = [...(catalog.provider_syncs ?? initial)].sort((a, b) =>
    a.provider === b.provider ? 0 : a.provider === 'libvio' ? -1 : 1,
  )

  function totals(provider: Provider) {
    const items = catalog.movies.filter((item) => item.provider === provider)
    return {
      movies: items.filter((item) => item.kind === 'movie').length,
      series: items.filter((item) => item.kind === 'series').length,
    }
  }

  function displayTime(value: string) {
    return new Date(value).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  }
</script>

<section class="provider-sync-panel" aria-label="供应商同步">
  <div class="provider-panel-heading">
    <div>
      <h2>供应商同步</h2>
      <p>单独更新对应片库。供应商不可用时保留原有条目。</p>
    </div>
    <Button.Root
      class="manual-button sync-all-button"
      onclick={() => onSync()}
      disabled={disabled ||
        syncingProviders.size > 0 ||
        providers.some((source) => source.status === 'syncing')}
    >
      <RefreshCw size={15} />同步全部
    </Button.Root>
  </div>
  <div class="provider-cards">
    {#each providers as source (source.provider)}
      {@const counts = totals(source.provider)}
      {@const busy = syncingProviders.has(source.provider) || source.status === 'syncing'}
      {@const status = busy ? 'syncing' : source.status}
      <article
        class="provider-card"
        class:provider-failed={status === 'failed' || status === 'partial'}
      >
        <div class="provider-card-top">
          <div>
            <h3>{names[source.provider]}</h3>
            <p>{counts.movies} 部电影 · {counts.series} 部电视剧</p>
          </div>
          <Button.Root
            class="sync-button"
            onclick={() => onSync(source.provider)}
            disabled={disabled || busy}
            aria-label={`同步 ${names[source.provider]}`}
          >
            <RefreshCw size={15} class={busy ? 'spin' : undefined} />{busy
              ? '正在同步'
              : '单独同步'}
          </Button.Root>
        </div>
        <div class="provider-sync-meta" role="status">
          <span class={`provider-status ${status}`}><i></i>{labels[status]}</span>
          {#if source.last_synced_at}<span>最近尝试 {displayTime(source.last_synced_at)}</span>{/if}
        </div>
        {#if !busy && source.failures.length}
          <div class="provider-sync-errors">
            {#each source.failures.slice(0, 2) as failure, index (index)}<p>{failure}</p>{/each}
            {#if source.failures.length > 2}<p>
                另有 {source.failures.length - 2} 项未完成，可再次单独同步。
              </p>{/if}
          </div>
        {/if}
      </article>
    {/each}
  </div>
</section>
