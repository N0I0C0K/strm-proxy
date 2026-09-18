<script lang="ts">
  import { Dialog } from 'bits-ui'
  import { Copy, Link2, X } from 'lucide-svelte'

  export let open = false
  export let credentials: string

  const address = `${window.location.origin}/dav/`
  const parsedAddress = new URL(address)
  const localAddress = ['localhost', '127.0.0.1', '[::1]'].includes(parsedAddress.hostname)
  let copyMessage = ''

  function currentAccount(value: string): [string, string] {
    try {
      const binary = atob(value.replace(/^Basic /i, ''))
      const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0))
      const decoded = new TextDecoder().decode(bytes)
      const separator = decoded.indexOf(':')
      return separator < 0 ? ['', ''] : [decoded.slice(0, separator), decoded.slice(separator + 1)]
    } catch {
      return ['', '']
    }
  }

  $: account = currentAccount(credentials)
  $: quickText = `协议：${parsedAddress.protocol.slice(0, -1).toUpperCase()} 地址：${parsedAddress.hostname} 端口：${parsedAddress.port || (parsedAddress.protocol === 'https:' ? '443' : '80')} 账号：${account[0]} 密码：${account[1]} 路径：${parsedAddress.pathname}`
  $: if (!open) copyMessage = ''

  function legacyCopy(text: string) {
    const field = document.createElement('textarea')
    field.value = text
    field.style.position = 'fixed'
    field.style.opacity = '0'
    document.body.appendChild(field)
    field.select()
    const copied = document.execCommand('copy')
    field.remove()
    if (!copied) throw new Error('复制失败')
  }

  async function copy() {
    try {
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(quickText)
        } catch {
          legacyCopy(quickText)
        }
      } else {
        legacyCopy(quickText)
      }
      copyMessage = '已复制爆米花识别文本'
    } catch {
      copyMessage = '复制失败，请手动选择下方文本'
    }
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Portal>
    <Dialog.Overlay class="modal-layer" />
    <Dialog.Content class="dav-dialog">
      <div class="dav-dialog-heading">
        <div class="dav-connect-heading"><Link2 size={19} /><div><Dialog.Title>WebDAV 连接</Dialog.Title><Dialog.Description>在播放器中添加此地址，或使用爆米花快捷识别。</Dialog.Description></div></div>
        <Dialog.Close class="detail-close" aria-label="关闭 WebDAV 连接信息"><X size={18} /></Dialog.Close>
      </div>
      <div class="dav-connect-main">
        <label for="dav-address">WebDAV 地址</label>
        <input id="dav-address" value={address} type="url" readonly spellcheck="false" />
        {#if localAddress}<small>当前地址仅供本机使用。其他设备连接时，请通过这台电脑的局域网 IP 或域名打开管理页，再复制连接信息。</small>{/if}
        <span class="dav-field-label">爆米花识别文本</span>
        <code class="dav-quick-text">{quickText}</code>
        <button class="dav-copy-button" type="button" onclick={copy}><Copy size={16} />复制完整连接信息</button>
        {#if copyMessage}<p class="dav-copy-message" role="status">{copyMessage}</p>{/if}
      </div>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
