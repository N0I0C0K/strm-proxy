<script lang="ts">
  import { Button, Select, ToggleGroup } from 'bits-ui'
  import { Check, ChevronDown, Grid2X2, List, MousePointer2, Search } from 'lucide-svelte'
  import { mediaTypeOptions, sortOptions, type LayoutMode, type MediaTypeFilter } from '../lib/types'

  export let layoutMode: LayoutMode
  export let mediaTypeFilter: MediaTypeFilter
  export let sortOrder: string
  export let query: string
  export let resultCount: number
</script>

<div class="catalog-toolbar">
  <div class="catalog-title">
    <h2>{mediaTypeFilter === 'series' ? '电视剧' : mediaTypeFilter === 'movie' ? '电影' : '影片'}</h2><span>{resultCount} 个结果</span>
    <span class="drag-hint"><MousePointer2 size={13} />拖动左侧选择框可连续选择</span>
  </div>
  <div class="toolbar-tools">
    <ToggleGroup.Root class="layout-toggle" type="single" value={layoutMode} onValueChange={(value) => { if (value === 'list' || value === 'grid') layoutMode = value }} aria-label="影片布局">
      <ToggleGroup.Item value="list" aria-label="列表布局" title="列表布局"><List size={16} /></ToggleGroup.Item>
      <ToggleGroup.Item value="grid" aria-label="网格布局" title="网格布局"><Grid2X2 size={16} /></ToggleGroup.Item>
    </ToggleGroup.Root>
    <Select.Root type="single" value={mediaTypeFilter} onValueChange={(value) => { if (value === 'all' || value === 'movie' || value === 'series') mediaTypeFilter = value }} items={mediaTypeOptions}>
      <Select.Trigger class="sort-box type-filter" aria-label="影片类型筛选"><Select.Value /><ChevronDown size={14} /></Select.Trigger>
      <Select.Portal><Select.Content class="select-content" sideOffset={6}><Select.Viewport>
        {#each mediaTypeOptions as option}
          <Select.Item class="select-item" value={option.value} label={option.label}>
            {#snippet children({ selected: itemSelected })}<span>{option.label}</span>{#if itemSelected}<Check size={14} />{/if}{/snippet}
          </Select.Item>
        {/each}
      </Select.Viewport></Select.Content></Select.Portal>
    </Select.Root>
    <Select.Root type="single" value={sortOrder} onValueChange={(value) => (sortOrder = value)} items={sortOptions}>
      <Select.Trigger class="sort-box" aria-label="影片排序"><span class="sort-label">排序</span><Select.Value /><ChevronDown size={14} /></Select.Trigger>
      <Select.Portal><Select.Content class="select-content" sideOffset={6}><Select.Viewport>
        {#each sortOptions as option}
          <Select.Item class="select-item" value={option.value} label={option.label}>
            {#snippet children({ selected: itemSelected })}<span>{option.label}</span>{#if itemSelected}<Check size={14} />{/if}{/snippet}
          </Select.Item>
        {/each}
      </Select.Viewport></Select.Content></Select.Portal>
    </Select.Root>
    <label class="search-box">
      <Search size={18} /><input bind:value={query} placeholder="搜索片名或年份" aria-label="搜索影片" />
      {#if query}<Button.Root onclick={() => (query = '')} aria-label="清空搜索">×</Button.Root>{/if}
    </label>
  </div>
</div>
