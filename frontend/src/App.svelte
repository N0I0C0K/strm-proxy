<script lang="ts">
  import LoginPage from './components/LoginPage.svelte'
  import LibraryPage from './pages/LibraryPage.svelte'
  import { adminApi, basicCredentials } from './lib/admin-api'
  import type { Catalog } from './lib/types'

  let credentials = ''
  let catalog: Catalog | null = null
  let loading = false
  let loginError = ''

  async function login(username: string, password: string) {
    loading = true
    loginError = ''
    const nextCredentials = basicCredentials(username, password)
    try {
      catalog = await adminApi<Catalog>(nextCredentials, '/media')
      credentials = nextCredentials
    } catch (error) {
      credentials = ''
      loginError = error instanceof Error ? error.message : '无法连接管理服务'
    } finally {
      loading = false
    }
  }

  function logout() {
    credentials = ''
    catalog = null
  }
</script>

<svelte:head>
  <title>片库控制台 · STRM Proxy</title>
  <meta name="description" content="管理自动发现、人工保留与隐藏的 STRM 影片资源。" />
</svelte:head>

{#if catalog}
  <LibraryPage {credentials} initialCatalog={catalog} onLogout={logout} />
{:else}
  <LoginPage {loading} error={loginError} onLogin={login} />
{/if}
