<script lang="ts">
  import { Button, Dialog } from 'bits-ui'
  import { Check, LoaderCircle, RotateCcw, Waypoints } from 'lucide-svelte'
  import type { Media, PlaybackRoutes } from '../lib/types'

  export let open = false
  export let media: Media | null = null
  export let routes: PlaybackRoutes | null = null
  export let busy = false
  export let error = ''
  export let onSelect: (routeName: string) => void
  export let onRestoreAuto: () => void

  function currentSummary(data: PlaybackRoutes): string {
    if (data.manual_override && data.selected_route_name) {
      return `已固定为 ${data.selected_route_name}`
    }
    if (data.cached_source === 'tos') return '当前缓存为 TOS 直连'
    if (data.cached_source === 'member') return '当前缓存为会员直连'
    if (data.cached_source === 'hls' && data.selected_line !== null) {
      const name = data.selected_route_name ? `（${data.selected_route_name}）` : ''
      return `当前自动缓存为线路 ${data.selected_line + 1}${name}`
    }
    return '当前由服务端自动探索'
  }

  function routeSubtitle(kind: string, name: string | null): string {
    if (kind === 'url3') return '直连地址；按线路序号匹配各集，播放时验证'
    if (kind === 'tos') return '站点直连；播放时重新获取并验证'
    if (kind === 'member') return '需要站点验证码，暂不能固定'
    return name ? `稳定标识 #${name}` : '上游未提供线路名称，不能固定'
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Portal>
    <Dialog.Overlay class="modal-layer" />
    <Dialog.Content class="route-dialog">
      <div class="import-heading">
        <div class="dialog-icon route"><Waypoints size={20} /></div>
        <div>
          <Dialog.Title>选择播放线路</Dialog.Title>
          <Dialog.Description>
            {media ? `《${media.title}》` : '影片'}的播放来源取自网站；{routes?.media_kind === 'series' ? '选择后应用于整部剧，每集重新获取对应来源。' : '选择后会覆盖该影片的自动选择。'}
          </Dialog.Description>
        </div>
      </div>

      {#if busy && !routes}
        <div class="route-loading"><LoaderCircle class="spin" size={20} />正在读取实时线路…</div>
      {:else if error && !routes}
        <p class="form-error route-error" role="alert">{error}</p>
      {:else if routes}
        <div class="route-status">
          <span class:manual={routes.manual_override}></span>
          <strong>{currentSummary(routes)}</strong>
          {#if routes.media_kind === 'series'}<small>列表取自第 1 集；固定后作用于整部剧</small>{/if}
        </div>

        <div class="route-options" aria-label="可用播放线路">
          {#each routes.routes as route}
            <button
              type="button"
              class:active={route.key === routes.selected_route_key}
              disabled={busy || !routes.cache_enabled || !route.selectable}
              onclick={() => onSelect(route.key)}
            >
              <span class="route-number">{route.line + 1}</span>
              <span class="route-copy">
                <strong>线路 {route.line + 1}{route.name ? ` · ${route.name}` : ''}</strong>
                <small>{routeSubtitle(route.kind, route.name)}</small>
              </span>
              {#if route.key === routes.selected_route_key}<Check size={17} />{/if}
            </button>
          {/each}
        </div>

        <p class="route-note">HLS 按线路名称匹配；直连线路按序号匹配。会员线路需要验证码，暂不能固定。</p>

        {#if !routes.cache_enabled}
          <p class="form-error route-error">播放决策缓存已由环境变量关闭，无法保存人工线路。</p>
        {:else if error}
          <p class="form-error route-error" role="alert">{error}</p>
        {/if}
      {/if}

      <div class="dialog-actions route-actions">
        <Button.Root
          class="restore-route"
          disabled={busy || !routes?.cache_enabled}
          onclick={onRestoreAuto}
        >
          <RotateCcw size={15} />恢复自动
        </Button.Root>
        <Dialog.Close disabled={busy}>关闭</Dialog.Close>
      </div>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
