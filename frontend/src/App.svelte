<script lang="ts">
  import { onMount } from 'svelte'
  import LoginPage from './components/LoginPage.svelte'
  import LibraryPage from './pages/LibraryPage.svelte'
  import { AdminApiError, adminApi, basicCredentials } from './lib/admin-api'
  import type { Catalog } from './lib/types'

  const sessionKey = 'strm-proxy.admin.credentials'
  let credentials = ''
  let catalog: Catalog | null = null
  let loading = false
  let restoring = true
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
    let saved = ''
    try {
      saved = sessionStorage.getItem(sessionKey) ?? ''
    } catch {
      // Continue with the login form when browser storage is unavailable.
    }
    if (!saved) {
      restoring = false
      return
    }
    adminApi<Catalog>(saved, '/media')
      .then((result) => {
        credentials = saved
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

{#if catalog}
  <LibraryPage {credentials} initialCatalog={catalog} onLogout={logout} onCredentialsChanged={credentialsChanged} />
{:else}
  <LoginPage loading={loading || restoring} error={loginError} onLogin={login} />
{/if}
