<script lang="ts">
  import { Button } from 'bits-ui'
  import { CirclePlus, Film, LoaderCircle, LogOut, RefreshCw } from 'lucide-svelte'

  export let syncing = false
  export let onSync: () => void
  export let onManualImport: () => void
  export let onLogout: () => void
</script>

<header class="topbar">
  <div class="brand-lockup">
    <div class="brand-mark small"><Film size={20} strokeWidth={2.2} /></div>
    <div><strong>片库控制台</strong><span>STRM Proxy</span></div>
  </div>
  <div class="topbar-actions">
    <span class="service-status"><i></i> 服务正常</span>
    <Button.Root class="icon-button" onclick={onLogout} aria-label="退出管理界面" title="退出"><LogOut size={18} /></Button.Root>
  </div>
</header>

<section class="page-heading">
  <div>
    <p class="eyebrow">影片发现</p>
    <h1>管理你的流动片库</h1>
    <p>自动池保持最新，人工保留不会被轮换，隐藏内容不会出现在 WebDAV。</p>
  </div>
  <div class="heading-actions">
    <Button.Root class="manual-button" onclick={onManualImport}><CirclePlus size={17} />手动添加</Button.Root>
    <Button.Root class="sync-button" onclick={onSync} disabled={syncing}>
      <RefreshCw class={syncing ? 'spin' : undefined} size={17} />{syncing ? '正在同步' : '立即同步'}
    </Button.Root>
  </div>
</section>
