<script lang="ts">
  import { Button, Dialog } from 'bits-ui'
  import { CirclePlus, ExternalLink, Film, Link2, LoaderCircle, Tv } from 'lucide-svelte'
  import type { ManualImportResult } from '../lib/types'

  export let open = false
  export let onImport: (url: string) => Promise<ManualImportResult>
  export let onCatalogImported: (result: ManualImportResult) => void

  let url = ''
  let busy = false
  let error = ''
  let result: ManualImportResult | null = null

  $: if (open) {
    error = ''
    result = null
  }

  async function importUrl() {
    if (!url.trim() || busy) return
    busy = true
    error = ''
    result = null
    try {
      result = await onImport(url.trim())
      if (result.catalog) onCatalogImported(result)
    } catch (caught) {
      error = caught instanceof Error ? caught.message : '无法识别这个网址'
    } finally {
      busy = false
    }
  }

  function imageFailed(event: Event) {
    ;(event.currentTarget as HTMLImageElement).style.display = 'none'
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Portal>
    <Dialog.Overlay class="modal-layer" />
    <Dialog.Content class="import-dialog">
      <div class="import-heading">
        <div class="dialog-icon import"><Link2 size={20} /></div>
        <div>
          <Dialog.Title>通过网址添加</Dialog.Title>
          <Dialog.Description>粘贴雪落影视的详情页。电影和电视剧都会直接加入片库并设为人工保留。</Dialog.Description>
        </div>
      </div>
      <form class="import-form" onsubmit={(event) => { event.preventDefault(); importUrl() }}>
        <label for="manual-url">影片详情页网址</label>
        <div class="url-field">
          <Link2 size={17} />
          <input id="manual-url" bind:value={url} type="url" placeholder="https://www.xlys02.com/…/27085.htm" required autocomplete="url" />
        </div>
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
        {#if result}
          <div class:series-result={result.kind === 'series'} class="import-result" aria-live="polite">
            <div class="result-cover">
              {#if result.cover_url}<img src={result.cover_url} alt="" referrerpolicy="no-referrer" onerror={imageFailed} />{:else}<Film size={22} />{/if}
            </div>
            <div>
              <span class="result-kind">{result.kind === 'series' ? '已添加电视剧' : '已添加电影'}</span>
              <strong>{result.title}{result.year ? ` (${result.year})` : ''}</strong>
              <p>{result.message}</p>
              {#if result.kind === 'series'}<small><Tv size={13} />当前 {result.available_episode_count} 集{result.declared_episode_count ? ` / 计划 ${result.declared_episode_count} 集` : ''}</small>{/if}
            </div>
            <a href={result.source_url} target="_blank" rel="noreferrer" aria-label="打开来源页"><ExternalLink size={16} /></a>
          </div>
        {/if}
        <div class="dialog-actions">
          <Dialog.Close disabled={busy}>关闭</Dialog.Close>
          <Button.Root class="confirm-import" type="submit" disabled={busy || !url.trim()}>
            {#if busy}<LoaderCircle class="spin" size={16} />{:else}<CirclePlus size={16} />{/if}
            {busy ? '正在识别…' : '识别并添加'}
          </Button.Root>
        </div>
      </form>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
