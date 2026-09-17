<script lang="ts">
  export let page: number
  export let totalPages: number
  export let totalItems: number
  export let pageSize: number
  export let onPageChange: (page: number) => void

  $: firstItem = (page - 1) * pageSize + 1
  $: lastItem = Math.min(page * pageSize, totalItems)
</script>

{#if totalItems > pageSize}
  <nav class="catalog-pagination" aria-label="片库分页">
    <span>显示 {firstItem}–{lastItem} / {totalItems} 部</span>
    <div class="pagination-buttons">
      <button type="button" disabled={page === 1} onclick={() => onPageChange(1)}>首页</button>
      <button type="button" disabled={page === 1} onclick={() => onPageChange(page - 1)}>上一页</button>
      <strong aria-live="polite">{page} / {totalPages}</strong>
      <button type="button" disabled={page === totalPages} onclick={() => onPageChange(page + 1)}>下一页</button>
      <button type="button" disabled={page === totalPages} onclick={() => onPageChange(totalPages)}>末页</button>
    </div>
  </nav>
{/if}
