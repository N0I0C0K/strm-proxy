<script lang="ts">
  import { onMount } from 'svelte'
  import { Film, LoaderCircle } from 'lucide-svelte'
  import LoginPage from './components/LoginPage.svelte'
  import LibraryPage from './pages/LibraryPage.svelte'
  import { AdminApiError, adminApi, basicCredentials } from './lib/admin-api'
  import type { Catalog } from './lib/types'

  const sessionKey = 'strm-proxy.admin.credentials'
  function readSession(): string {
    try {
      return sessionStorage.getItem(sessionKey) ?? ''
    } catch {
      return ''
    }
  }

  const savedOnLoad = readSession()
  let credentials = ''
  let catalog: Catalog | null = null
  let loading = false
  let restoring = Boolean(savedOnLoad)
  let loginError = ''

  function saveSession(value: string | null) {
    try {
      if (value) sessionStorage.setItem(sessionKey, value)
      else sessionStorage.removeItem(sessionKey)
    } catch {
      // Login remains usable when browser storage is unavailable.
    }
  }

  onMount(() => {
    if (!savedOnLoad) return
    adminApi<Catalog>(savedOnLoad, '/media')
      .then((result) => {
        credentials = savedOnLoad
        catalog = result
      })
      .catch((error) => {
        if (error instanceof AdminApiError && error.status === 401) {
          saveSession(null)
          loginError = '登录已失效，请重新输入账号密码'
        } else {
          loginError = error instanceof Error ? error.message : '无法连接管理服务'
        }
      })
      .finally(() => { restoring = false })
  })

  async function login(username: string, password: string) {
    if (loading || restoring) return
    loading = true
    loginError = ''
    const nextCredentials = basicCredentials(username, password)
    try {
      catalog = await adminApi<Catalog>(nextCredentials, '/media')
      credentials = nextCredentials
      saveSession(nextCredentials)
    } catch (error) {
      credentials = ''
      saveSession(null)
      loginError = error instanceof Error ? error.message : '无法连接管理服务'
    } finally {
      loading = false
    }
  }

  function logout() {
    saveSession(null)
    credentials = ''
    catalog = null
  }

  function credentialsChanged(username: string, password: string) {
    credentials = basicCredentials(username, password)
    saveSession(credentials)
  }
</script>

<svelte:head>
  <title>片库控制台 · STRM Proxy</title>
  <meta name="description" content="管理自动发现、人工保留与隐藏的 STRM 影片资源。" />
</svelte:head>

{#if restoring}
  <div class="restore-shell" aria-busy="true">
    <header class="topbar">
      <div class="brand-lockup">
        <div class="brand-mark small"><Film size={20} strokeWidth={2.2} /></div>
        <div><strong>片库控制台</strong><span>STRM Proxy</span></div>
      </div>
    </header>
    <main class="restore-workspace">
      <div class="restore-heading"></div>
      <div class="restore-metrics"></div>
      <div class="restore-panel"></div>
      <p class="restore-status"><LoaderCircle class="spin" size={16} />正在打开片库…</p>
    </main>
  </div>
{:else if catalog}
  <LibraryPage {credentials} initialCatalog={catalog} onLogout={logout} onCredentialsChanged={credentialsChanged} />
{:else}
  <LoginPage {loading} error={loginError} onLogin={login} />
{/if}
