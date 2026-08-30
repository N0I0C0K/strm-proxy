<script lang="ts">
  import { Button, Checkbox, Select, Toolbar } from 'bits-ui'
  import { Check, ChevronDown, EyeOff, LoaderCircle, Search, Star, Trash2 } from 'lucide-svelte'
  import { policyOptions, type LayoutMode, type Media, type Policy } from '../lib/types'

  export let media: Media[]
  export let layoutMode: LayoutMode
  export let selected: Set<number>
  export let updating: Set<number>
  export let bulkBusy = false
  export let onSelectionChange: (selection: Set<number>) => void
  export let onUpdatePolicy: (media: Media, policy: Policy) => void
  export let onBulkPolicy: (policy: Policy) => void
  export let onDelete: () => void
  export let onResetFilters: () => void

  let dragPointerId: number | null = null
  let dragValue = true
  let suppressedClickId: number | null = null

  $: allResultsSelected = media.length > 0 && media.every((item) => selected.has(item.xlys_id))
  $: selectedResultCount = media.filter((item) => selected.has(item.xlys_id)).length

  function setSelected(xlysId: number, value: boolean) {
    if (selected.has(xlysId) === value) return
    const next = new Set(selected)
    value ? next.add(xlysId) : next.delete(xlysId)
    onSelectionChange(next)
  }

  function startDragSelection(event: PointerEvent, xlysId: number) {
    if (event.pointerType === 'mouse' && event.button !== 0) return
    event.preventDefault()
    dragPointerId = event.pointerId
    dragValue = !selected.has(xlysId)
    suppressedClickId = xlysId
    ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
    setSelected(xlysId, dragValue)
  }

  function continueDragSelection(event: PointerEvent) {
    if (event.pointerId !== dragPointerId) return
    const row = [...document.querySelectorAll<HTMLElement>('[data-movie-id]')].find((element) => {
      const bounds = element.getBoundingClientRect()
      return event.clientX >= bounds.left && event.clientX <= bounds.right && event.clientY >= bounds.top && event.clientY <= bounds.bottom
    })
    const xlysId = Number(row?.dataset.movieId)
    if (Number.isInteger(xlysId)) setSelected(xlysId, dragValue)
  }

  function stopDragSelection(event?: PointerEvent) {
    if (event && event.pointerId !== dragPointerId) return
    dragPointerId = null
  }

  function suppressPointerClick(event: MouseEvent, xlysId: number) {
    if (event.detail === 0 || suppressedClickId !== xlysId) return
    event.preventDefault()
    event.stopPropagation()
    suppressedClickId = null
  }

  function toggleAllResults() {
    const next = new Set(selected)
    if (allResultsSelected) media.forEach((item) => next.delete(item.xlys_id))
    else media.forEach((item) => next.add(item.xlys_id))
    onSelectionChange(next)
  }

  function clearSelection() {
    onSelectionChange(new Set())
  }

  function formatDate(value: string | null) {
    if (!value) return '未知日期'
    return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(`${value}T00:00:00`))
  }

  function imageFailed(event: Event) {
    ;(event.currentTarget as HTMLImageElement).style.display = 'none'
  }
</script>

<svelte:window onpointermove={continueDragSelection} onpointerup={stopDragSelection} onpointercancel={stopDragSelection} onblur={() => stopDragSelection()} />

{#if selected.size}
  <div class="bulk-bar" aria-live="polite">
    <strong>已选择 {selected.size} 部</strong>
    <Toolbar.Root class="bulk-actions" aria-label="批量影片操作">
      <Toolbar.Button onclick={() => onBulkPolicy('keep')} disabled={bulkBusy}><Check size={15} />设为保留</Toolbar.Button>
      <Toolbar.Button onclick={() => onBulkPolicy('hidden')} disabled={bulkBusy}><EyeOff size={15} />隐藏</Toolbar.Button>
      <Toolbar.Button class="danger" onclick={onDelete} disabled={bulkBusy}><Trash2 size={15} />删除</Toolbar.Button>
      <Toolbar.Button class="clear-selection" onclick={clearSelection}>取消选择</Toolbar.Button>
    </Toolbar.Root>
  </div>
{/if}

{#if layoutMode === 'list'}
  <div class="table-head">
    <Checkbox.Root class="check-control" checked={allResultsSelected} indeterminate={selectedResultCount > 0 && !allResultsSelected} onCheckedChange={toggleAllResults} aria-label="选择当前全部结果" title="选择当前结果">
      {#snippet children({ checked, indeterminate })}{#if checked}<Check size={12} />{:else if indeterminate}<span class="minus">−</span>{/if}{/snippet}
    </Checkbox.Root>
    <span>影片</span><span>更新时间</span><span>片库策略</span>
  </div>
{/if}

{#if media.length}
  <div class:movie-list={layoutMode === 'list'} class:movie-grid={layoutMode === 'grid'} class:drag-selecting={dragPointerId !== null}>
    {#each media as item (item.xlys_id)}
      <article class:movie-row={layoutMode === 'list'} class:grid-card={layoutMode === 'grid'} class:selected={selected.has(item.xlys_id)} data-movie-id={item.xlys_id}>
        <Checkbox.Root class="check-control row-check" checked={selected.has(item.xlys_id)} onCheckedChange={(checked) => setSelected(item.xlys_id, checked)} onpointerdown={(event) => startDragSelection(event, item.xlys_id)} onclick={(event) => suppressPointerClick(event, item.xlys_id)} aria-label={`选择《${item.title}》`} title="按住并拖动可连续选择" data-select-id={item.xlys_id}>
          {#snippet children({ checked })}{#if checked}<Check size={12} />{/if}{/snippet}
        </Checkbox.Root>
        <a class="poster" href={item.play_page_url} target="_blank" rel="noreferrer" aria-label={`打开《${item.title}》播放页`}>
          <span>{item.title.slice(0, 1)}</span>
          {#if item.cover_url}<img src={item.cover_url} alt="" loading="lazy" referrerpolicy="no-referrer" onerror={imageFailed} />{/if}
        </a>
        <div class="movie-copy">
          <div class="movie-title-line"><h3>{item.title}</h3></div>
          <div class="media-tags">
            <span class:series-kind={item.kind === 'series'} class="media-kind">{item.kind === 'series' ? '电视剧' : '电影'}</span>
            {#if item.year}<span>{item.year}</span>{/if}
            {#if item.douban_rating !== null}<span class="rating-chip"><Star size={10} fill="currentColor" />{item.douban_rating.toFixed(1)}</span>{/if}
          </div>
          <p>{item.dav_filename}</p>
        </div>
        <div class="updated-cell"><span>{formatDate(item.source_updated_on)}</span><small>来源更新</small></div>
        <Select.Root type="single" value={item.policy} onValueChange={(value) => onUpdatePolicy(item, value as Policy)} items={policyOptions} disabled={updating.has(item.xlys_id)}>
          <Select.Trigger class={`policy-control${updating.has(item.xlys_id) ? ' busy' : ''}`} aria-label={`设置《${item.title}》的片库策略`}>
            <span class={`policy-dot ${item.policy}`}>{#if item.policy === 'keep'}<Check size={13} />{:else if item.policy === 'hidden'}<EyeOff size={13} />{/if}</span>
            <Select.Value />
            {#if updating.has(item.xlys_id)}<LoaderCircle class="spin policy-loader" size={15} />{:else}<ChevronDown class="select-chevron" size={15} />{/if}
          </Select.Trigger>
          <Select.Portal><Select.Content class="select-content policy-menu" sideOffset={5}><Select.Viewport>
            {#each policyOptions as option}
              <Select.Item class="select-item" value={option.value} label={option.label}>
                {#snippet children({ selected: itemSelected })}<span>{option.label}</span>{#if itemSelected}<Check size={14} />{/if}{/snippet}
              </Select.Item>
            {/each}
          </Select.Viewport></Select.Content></Select.Portal>
        </Select.Root>
      </article>
    {/each}
  </div>
{:else}
  <div class="empty-state">
    <Search size={24} /><h3>没有匹配的影片</h3><p>换一个关键词、类型或查看其他策略。</p>
    <Button.Root onclick={onResetFilters}>查看全部影片</Button.Root>
  </div>
{/if}
