<script lang="ts">
  import Board from '../components/Board.svelte';
</script>

<div class="board-page">
  <Board />
</div>

<style>
  .board-page {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
</style>
