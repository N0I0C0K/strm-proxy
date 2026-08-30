<script lang="ts">
  import { Button } from 'bits-ui'
  import { Film, KeyRound, LoaderCircle, ShieldCheck, Sparkles } from 'lucide-svelte'

  export let loading = false
  export let error = ''
  export let onLogin: (username: string, password: string) => void

  let username = 'demo'
  let password = 'demo'
</script>

<main class="login-shell">
  <section class="login-brand" aria-label="产品介绍">
    <div class="brand-mark"><Film size={24} strokeWidth={2.2} /></div>
    <p class="eyebrow">STRM PROXY</p>
    <h1>让片库保持<br />新鲜，也保持克制。</h1>
    <p class="login-intro">自动收录最新影片，把真正喜欢的长期保留，其余随片库自然流动。</p>
    <div class="flow-note"><Sparkles size={18} /><span>发现、筛选、播放，一条链路完成</span></div>
  </section>

  <section class="login-panel">
    <form class="login-card" onsubmit={(event) => { event.preventDefault(); onLogin(username, password) }}>
      <div class="login-icon"><KeyRound size={22} /></div>
      <div>
        <p class="eyebrow">管理入口</p>
        <h2>登录片库</h2>
        <p class="muted">使用 WebDAV 的用户名和密码。</p>
      </div>
      <label><span>用户名</span><input bind:value={username} autocomplete="username" required /></label>
      <label><span>密码</span><input bind:value={password} type="password" autocomplete="current-password" required /></label>
      {#if error}<p class="form-error" role="alert">{error}</p>{/if}
      <Button.Root class="primary-button" type="submit" disabled={loading}>
        {#if loading}<LoaderCircle class="spin" size={18} />{/if}
        {loading ? '正在连接…' : '进入控制台'}
      </Button.Root>
      <p class="secure-note"><ShieldCheck size={15} /> 凭据仅保留在当前页面内存中</p>
    </form>
  </section>
</main>
