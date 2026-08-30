<script lang="ts">
  import { Button } from 'bits-ui'
  import type { Catalog, Policy } from '../lib/types'

  export let catalog: Catalog
  export let policyFilter: 'all' | Policy
</script>

<section class="metrics" aria-label="片库概览">
  <Button.Root class={policyFilter === 'all' ? 'active' : ''} onclick={() => (policyFilter = 'all')}><span>全部内容</span><strong>{catalog.counts.total}</strong><small>{catalog.counts.movies} 部电影 · {catalog.counts.series} 部电视剧</small></Button.Root>
  <Button.Root class={policyFilter === 'auto' ? 'active' : ''} onclick={() => (policyFilter = 'auto')}><span>自动发现</span><strong>{catalog.counts.automatic}</strong><small>近 4 年评分 &gt; 7 + 最近 {catalog.recent_limit}</small></Button.Root>
  <Button.Root class={policyFilter === 'keep' ? 'active' : ''} onclick={() => (policyFilter = 'keep')}><span>人工保留</span><strong>{catalog.counts.kept}</strong><small>始终在片库</small></Button.Root>
  <Button.Root class={policyFilter === 'hidden' ? 'active' : ''} onclick={() => (policyFilter = 'hidden')}><span>已隐藏</span><strong>{catalog.counts.hidden}</strong><small>不提供给爆米花</small></Button.Root>
</section>
