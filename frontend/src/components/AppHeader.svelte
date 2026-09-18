<script lang="ts">
  import { Button } from 'bits-ui'
  import { CirclePlus, Film, Link2, LogOut, RefreshCw, Settings } from 'lucide-svelte'

  export let syncing = false
  export let recentRefreshing = false
  export let itemRefreshing = false
  export let recentSeriesCount = 0
  export let onSync: () => void
  export let onRefreshRecent: () => void
  export let onManualImport: () => void
  export let onLogout: () => void
  export let onAccount: () => void
  export let onWebDav: () => void
</script>

<header class="topbar">
  <div class="brand-lockup">
    <div class="brand-mark small"><Film size={20} strokeWidth={2.2} /></div>
    <div><strong>片库控制台</strong><span>STRM Proxy</span></div>
  </div>
  <div class="topbar-actions">
    <span class="service-status"><i></i> 服务正常</span>
    <Button.Root class="dav-topbar-button" onclick={onWebDav} aria-label="查看 WebDAV 连接信息" title="WebDAV 连接"><Link2 size={16} /><span>WebDAV</span></Button.Root>
    <Button.Root class="icon-button" onclick={onAccount} aria-label="修改账号密码" title="账号设置"><Settings size={18} /></Button.Root>
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
    <Button.Root class="manual-button" onclick={onRefreshRecent} disabled={recentRefreshing || syncing || itemRefreshing || recentSeriesCount === 0} title="刷新近 30 天播放过的电视剧详情页和分集">
      <RefreshCw class={recentRefreshing ? 'spin' : undefined} size={17} />{recentRefreshing ? '正在刷新' : `刷新最近看过的电视剧（${recentSeriesCount}）`}
    </Button.Root>
    <div class="sync-tooltip-anchor">
      <Button.Root class="sync-button" onclick={onSync} disabled={syncing || recentRefreshing || itemRefreshing} aria-describedby="sync-tooltip">
        <RefreshCw class={syncing ? 'spin' : undefined} size={17} />{syncing ? '正在同步' : '立即同步'}
      </Button.Root>
      <div class="sync-tooltip" id="sync-tooltip" role="tooltip">
        <strong>同步自动片库</strong>
        <span>重新发现电影和电视剧，加入新条目，更新已发现条目的信息及可获取的分集。</span>
        <span>本次未发现的自动条目会退出片库；近 30 天看过的电视剧会保留，人工保留和隐藏策略不变。</span>
      </div>
    </div>
  </div>
</section>
