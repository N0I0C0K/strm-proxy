<script lang="ts">
  import { AlertDialog } from 'bits-ui'
  import { LoaderCircle, Trash2 } from 'lucide-svelte'

  export let open = false
  export let count = 0
  export let busy = false
  export let onDelete: () => void
</script>

<AlertDialog.Root bind:open>
  <AlertDialog.Portal>
    <AlertDialog.Overlay class="modal-layer" />
    <AlertDialog.Content class="confirm-dialog">
      <div class="dialog-icon"><Trash2 size={20} /></div>
      <AlertDialog.Title>删除 {count} 部影片？</AlertDialog.Title>
      <AlertDialog.Description>这些记录会从 SQLite 中删除。如果影片仍在来源最新列表，下次同步时可能再次出现；如需长期排除，请使用“隐藏”。</AlertDialog.Description>
      <div class="dialog-actions">
        <AlertDialog.Cancel disabled={busy}>取消</AlertDialog.Cancel>
        <AlertDialog.Action class="confirm-delete" onclick={(event) => { event.preventDefault(); onDelete() }} disabled={busy}>
          {#if busy}<LoaderCircle class="spin" size={16} />{/if}确认删除
        </AlertDialog.Action>
      </div>
    </AlertDialog.Content>
  </AlertDialog.Portal>
</AlertDialog.Root>
