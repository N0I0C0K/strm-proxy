<script lang="ts">
  import { Dialog } from 'bits-ui'
  import { ExternalLink, LoaderCircle, RefreshCw, Waypoints, X } from 'lucide-svelte'
  import { policyMeta, type Media, type MediaDetail, type Policy } from '../lib/types'

  export let open = false
  export let media: Media | null = null
  export let detail: MediaDetail | null = null
  export let loading = false
  export let busy = false
  export let error = ''
  export let notice = ''
  export let onRefresh: () => void
  export let onPolicyChange: (policy: Policy) => void
  export let onConfigureRoute: () => void

  const policies: Policy[] = ['auto', 'keep', 'hidden']

  function formatDate(value: string | null): string {
    return value ? value.slice(0, 10) : '暂无记录'
  }

  function formatDateTime(value: string | null): string {
    if (!value) return '暂无记录'
    const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`
    const parsed = new Date(normalized)
    return Number.isNaN(parsed.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    }).format(parsed)
  }

  function imageFailed(event: Event) {
    ;(event.currentTarget as HTMLImageElement).style.display = 'none'
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Portal>
    <Dialog.Overlay class="modal-layer" />
    <Dialog.Content class="media-detail-dialog">
      <div class="detail-topline">
        <span>影片详情</span>
        <Dialog.Close class="detail-close" aria-label="关闭详情"><X size={18} /></Dialog.Close>
      </div>

      {#if media}
        <div class="detail-hero">
          <div class="detail-poster">
            <span>{media.title.slice(0, 1)}</span>
            {#if media.cover_url}<img src={media.cover_url} alt="" referrerpolicy="no-referrer" onerror={imageFailed} />{/if}
          </div>
          <div class="detail-heading">
            <Dialog.Title>{media.title}</Dialog.Title>
            <Dialog.Description>{media.kind === 'series' ? '电视剧' : '电影'}{media.year ? ` · ${media.year}` : ''}{media.douban_rating !== null ? ` · 豆瓣 ${media.douban_rating.toFixed(1)}` : ''}</Dialog.Description>
            {#if media.kind === 'series'}
              <strong class="detail-episode-summary">已收录 {media.available_episode_count} 集{detail?.declared_episode_count ? ` / 共 ${detail.declared_episode_count} 集` : ''}</strong>
            {/if}
            <div class="detail-primary-actions">
              <button type="button" onclick={onRefresh} disabled={busy || loading}><RefreshCw class={busy ? 'spin' : undefined} size={15} />刷新影片</button>
              <button type="button" onclick={onConfigureRoute} disabled={busy || loading}><Waypoints size={15} />播放线路</button>
            </div>
          </div>
        </div>
      {:else}
        <Dialog.Title>影片详情</Dialog.Title>
        <Dialog.Description>影片已从片库移除。</Dialog.Description>
      {/if}

      {#if error}<p class="form-error detail-message" role="alert">{error}</p>{/if}
      {#if notice}<p class="page-notice detail-message" role="status">{notice}</p>{/if}

      {#if loading && !detail}
        <div class="route-loading"><LoaderCircle class="spin" size={20} />正在读取影片详情…</div>
      {:else if detail && media}
        <section class="detail-section">
          <h3>片库策略</h3>
          <div class="detail-policy-actions" aria-label="片库策略">
            {#each policies as policy}
              <button type="button" class:active={media.policy === policy} aria-pressed={media.policy === policy} disabled={busy} onclick={() => onPolicyChange(policy)}>{policyMeta[policy].short}</button>
            {/each}
          </div>
          <p class="detail-hint">{policyMeta[media.policy].label}</p>
        </section>

        <section class="detail-section">
          <h3>影片信息</h3>
          <dl class="detail-facts">
            <div><dt>来源更新</dt><dd>{formatDate(detail.source_updated_on)}</dd></div>
            <div><dt>上次检查</dt><dd>{formatDateTime(detail.last_checked_at)}</dd></div>
            <div><dt>最近播放</dt><dd>{formatDateTime(detail.last_watched_at)}</dd></div>
            <div><dt>来源 ID</dt><dd>{detail.xlys_id}</dd></div>
            {#if detail.kind === 'series'}<div><dt>季度</dt><dd>{detail.season_number ?? '未标注'}</dd></div>{/if}
            <div class="detail-wide"><dt>WebDAV 名称</dt><dd>{detail.dav_filename}</dd></div>
          </dl>
          <div class="detail-links">
            {#if detail.source_url}<a href={detail.source_url} target="_blank" rel="noreferrer">来源详情页 <ExternalLink size={14} /></a>{/if}
            <a href={detail.episodes[0]?.play_page_url ?? detail.play_page_url} target="_blank" rel="noreferrer">打开播放页 <ExternalLink size={14} /></a>
          </div>
        </section>

        {#if detail.kind === 'series'}
          <section class="detail-section">
            <h3>分集 <span>{detail.episodes.length} 集</span></h3>
            {#if detail.episodes.length}
              <div class="detail-episodes">
                {#each detail.episodes as episode (episode.source_index)}
                  <a href={episode.play_page_url} target="_blank" rel="noreferrer"><span>{episode.source_index + 1}</span>{episode.label}<ExternalLink size={13} /></a>
                {/each}
              </div>
            {:else}
              <p class="detail-hint">还没有收录分集，可尝试刷新影片。</p>
            {/if}
          </section>
        {/if}
      {/if}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
