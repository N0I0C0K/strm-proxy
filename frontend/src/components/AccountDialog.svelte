<script lang="ts">
  import { Dialog } from 'bits-ui'
  import { adminApi } from '../lib/admin-api'

  export let open = false
  export let credentials: string
  export let onCredentialsChanged: (username: string, password: string) => void

  let username = ''
  let currentPassword = ''
  let password = ''
  let confirmPassword = ''
  let busy = false
  let error = ''

  $: if (open && !username) {
    adminApi<{ username: string }>(credentials, '/credentials')
      .then((result) => { if (open) username = result.username })
      .catch((caught) => { error = caught instanceof Error ? caught.message : '读取账号失败' })
  }
  $: if (!open) {
    username = ''
    currentPassword = ''
    password = ''
    confirmPassword = ''
    error = ''
  }

  async function save() {
    if (busy) return
    error = ''
    if (password !== confirmPassword) {
      error = '两次输入的新密码不一致'
      return
    }
    busy = true
    try {
      await adminApi<{ username: string }>(credentials, '/credentials', {
        method: 'PUT',
        body: JSON.stringify({ username: username.trim(), current_password: currentPassword, password }),
      })
      onCredentialsChanged(username.trim(), password)
      close()
    } catch (caught) {
      error = caught instanceof Error ? caught.message : '保存账号失败'
    } finally {
      busy = false
    }
  }

  function close() {
    open = false
    username = ''
    currentPassword = ''
    password = ''
    confirmPassword = ''
    error = ''
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Portal>
    <Dialog.Overlay class="modal-layer" />
    <Dialog.Content class="account-dialog">
      <Dialog.Title>修改管理账号</Dialog.Title>
      <Dialog.Description>管理页面和 WebDAV 共用此账号。修改后请更新其他设备的连接设置。</Dialog.Description>
      <form class="account-form" onsubmit={(event) => { event.preventDefault(); save() }}>
        <label>用户名<input bind:value={username} autocomplete="username" required maxlength="255" /></label>
        <label>当前密码<input bind:value={currentPassword} type="password" autocomplete="current-password" required /></label>
        <label>新密码<input bind:value={password} type="password" autocomplete="new-password" required minlength="8" /></label>
        <label>确认新密码<input bind:value={confirmPassword} type="password" autocomplete="new-password" required minlength="8" /></label>
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
        <div class="dialog-actions">
          <button type="button" onclick={close}>取消</button>
          <button class="confirm-import" type="submit" disabled={busy}>{busy ? '正在保存…' : '保存账号'}</button>
        </div>
      </form>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
