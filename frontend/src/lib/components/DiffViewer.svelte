<script lang="ts">
  import { onMount } from 'svelte';
  import { repos } from '../api/client';
  import type { DiffResponse, FileDiff } from '../api/types';

  export let repoId: string;
  export let baseBranch: string;
  export let headBranch: string;
  export let refreshKey: number = 0;  // Increment to force reload

  let diff: DiffResponse | null = null;
  let loading = true;
  let error: string | null = null;
  let expandedFiles: Set<string> = new Set();

  $: if (repoId && baseBranch && headBranch && refreshKey >= 0) {
    loadDiff();
  }

  export function refresh() {
    loadDiff();
  }

  async function loadDiff() {
    loading = true;
    error = null;
    // Reset expanded files when diff changes
    expandedFiles = new Set();
    try {
      diff = await repos.diff(repoId, baseBranch, headBranch);
      // Auto-expand first few files
      if (diff.files.length > 0) {
        expandedFiles = new Set(diff.files.slice(0, 3).map(f => f.path));
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load diff';
      diff = null;
    } finally {
      loading = false;
    }
  }

  function toggleFile(path: string) {
    if (expandedFiles.has(path)) {
      expandedFiles.delete(path);
    } else {
      expandedFiles.add(path);
    }
    expandedFiles = expandedFiles;
  }

  function getStatusIcon(status: string): string {
    switch (status) {
      case 'added': return '+';
      case 'deleted': return '-';
      case 'modified': return '~';
      default: return '?';
    }
  }

  function getStatusClass(status: string): string {
    switch (status) {
      case 'added': return 'status-added';
      case 'deleted': return 'status-deleted';
      case 'modified': return 'status-modified';
      default: return '';
    }
  }

  function parseDiffLines(diffText: string): Array<{type: string; content: string; oldNum?: number; newNum?: number}> {
    if (!diffText) return [];

    const lines = diffText.split('\n');
    const result: Array<{type: string; content: string; oldNum?: number; newNum?: number}> = [];
    let oldLine = 0;
    let newLine = 0;

    for (const line of lines) {
      if (line.startsWith('@@')) {
        // Parse hunk header like @@ -1,5 +1,7 @@
        const match = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        if (match) {
          oldLine = parseInt(match[1]) - 1;
          newLine = parseInt(match[2]) - 1;
        }
        result.push({ type: 'hunk', content: line });
      } else if (line.startsWith('+')) {
        newLine++;
        result.push({ type: 'add', content: line.slice(1), newNum: newLine });
      } else if (line.startsWith('-')) {
        oldLine++;
        result.push({ type: 'del', content: line.slice(1), oldNum: oldLine });
      } else if (line.startsWith(' ') || line === '') {
        oldLine++;
        newLine++;
        result.push({ type: 'ctx', content: line.slice(1) || '', oldNum: oldLine, newNum: newLine });
      }
    }

    return result;
  }
</script>

{#if loading}
  <div class="diff-loading">
    <span class="spinner"></span>
    Loading diff...
  </div>
{:else if error}
  <div class="diff-error">
    {error}
  </div>
{:else if diff}
  <div class="diff-viewer">
    <div class="diff-header">
      <div class="diff-summary">
        <span class="branch-info">
          <code>{baseBranch}</code>
          <span class="arrow">...</span>
          <code>{headBranch}</code>
        </span>
        <span class="stats">
          <span class="commits">{diff.commit_count} commit{diff.commit_count !== 1 ? 's' : ''}</span>
          <span class="additions">+{diff.total_additions}</span>
          <span class="deletions">-{diff.total_deletions}</span>
          <button class="btn-refresh" on:click={refresh} title="Refresh diff">↻</button>
        </span>
      </div>
    </div>

    {#if diff.files.length === 0}
      <div class="no-changes">No file changes between these branches.</div>
    {:else}
      <div class="file-list">
        {#each diff.files as file}
          <div class="file-item">
            <button
              class="file-header"
              on:click={() => toggleFile(file.path)}
            >
              <span class="expand-icon">{expandedFiles.has(file.path) ? '▼' : '▶'}</span>
              <span class="file-status {getStatusClass(file.status)}">{getStatusIcon(file.status)}</span>
              <span class="file-path">{file.path}</span>
              <span class="file-stats">
                <span class="additions">+{file.additions}</span>
                <span class="deletions">-{file.deletions}</span>
              </span>
            </button>

            {#if expandedFiles.has(file.path)}
              <div class="file-diff">
                {#if file.status === 'deleted'}
                  <div class="file-deleted-notice">File was deleted</div>
                {:else if !file.diff}
                  <div class="no-diff">Binary file or no diff available</div>
                {:else}
                  <table class="diff-table">
                    <tbody>
                      {#each parseDiffLines(file.diff) as line}
                        {#if line.type === 'hunk'}
                          <tr class="hunk-row">
                            <td class="line-num"></td>
                            <td class="line-num"></td>
                            <td class="hunk-header">{line.content}</td>
                          </tr>
                        {:else}
                          <tr class="diff-row {line.type}">
                            <td class="line-num">{line.oldNum || ''}</td>
                            <td class="line-num">{line.newNum || ''}</td>
                            <td class="diff-content">
                              <span class="diff-prefix">{line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' '}</span>
                              <span class="diff-text">{line.content}</span>
                            </td>
                          </tr>
                        {/if}
                      {/each}
                    </tbody>
                  </table>
                {/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<style>
  .diff-loading, .diff-error {
    padding: 1rem;
    text-align: center;
    color: var(--text-muted, #6c7086);
  }

  .diff-error {
    color: var(--error-color, #f38ba8);
  }

  .spinner {
    display: inline-block;
    width: 16px;
    height: 16px;
    border: 2px solid var(--border-color, #45475a);
    border-top-color: var(--primary-color, #89b4fa);
    border-radius: 50%;
    animation: spin 1s linear infinite;
    margin-right: 0.5rem;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .diff-viewer {
    background: var(--surface-alt, #181825);
    border-radius: 8px;
    overflow: hidden;
  }

  .diff-header {
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border-color, #45475a);
    background: var(--surface-color, #1e1e2e);
  }

  .diff-summary {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .branch-info {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .branch-info code {
    background: var(--badge-bg, #313244);
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    font-size: 0.8rem;
    color: var(--text-color, #cdd6f4);
  }

  .arrow {
    color: var(--text-muted, #6c7086);
  }

  .stats {
    display: flex;
    gap: 0.75rem;
    font-size: 0.85rem;
  }

  .commits {
    color: var(--text-muted, #6c7086);
  }

  .additions {
    color: var(--success-color, #a6e3a1);
  }

  .deletions {
    color: var(--error-color, #f38ba8);
  }

  .btn-refresh {
    background: none;
    border: none;
    color: var(--text-muted, #6c7086);
    cursor: pointer;
    font-size: 1rem;
    padding: 0.2rem 0.4rem;
    border-radius: 4px;
    margin-left: 0.25rem;
  }

  .btn-refresh:hover {
    color: var(--text-color, #cdd6f4);
    background: var(--hover-color, #313244);
  }

  .no-changes {
    padding: 2rem;
    text-align: center;
    color: var(--text-muted, #6c7086);
  }

  .file-list {
    max-height: 400px;
    overflow-y: auto;
  }

  .file-item {
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .file-item:last-child {
    border-bottom: none;
  }

  .file-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    padding: 0.6rem 1rem;
    background: none;
    border: none;
    color: var(--text-color, #cdd6f4);
    cursor: pointer;
    text-align: left;
    font-size: 0.85rem;
  }

  .file-header:hover {
    background: var(--hover-color, #313244);
  }

  .expand-icon {
    width: 1rem;
    color: var(--text-muted, #6c7086);
    font-size: 0.7rem;
  }

  .file-status {
    font-weight: bold;
    width: 1rem;
    text-align: center;
  }

  .status-added { color: var(--success-color, #a6e3a1); }
  .status-deleted { color: var(--error-color, #f38ba8); }
  .status-modified { color: var(--warning-color, #f9e2af); }

  .file-path {
    flex: 1;
    font-family: monospace;
    word-break: break-all;
  }

  .file-stats {
    display: flex;
    gap: 0.5rem;
    font-size: 0.8rem;
  }

  .file-diff {
    background: var(--surface-alt, #181825);
    overflow-x: auto;
  }

  .file-deleted-notice, .no-diff {
    padding: 1rem;
    text-align: center;
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  .diff-table {
    width: 100%;
    border-collapse: collapse;
    font-family: monospace;
    font-size: 0.8rem;
  }

  .hunk-row td {
    padding: 0.3rem 0.5rem;
    background: var(--surface-color, #1e1e2e);
  }

  .hunk-header {
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  .line-num {
    width: 40px;
    min-width: 40px;
    padding: 0 0.5rem;
    text-align: right;
    color: var(--text-muted, #6c7086);
    user-select: none;
    border-right: 1px solid var(--border-color, #45475a);
    font-size: 0.75rem;
  }

  .diff-row.add {
    background: rgba(166, 227, 161, 0.1);
  }

  .diff-row.del {
    background: rgba(243, 139, 168, 0.1);
  }

  .diff-content {
    padding: 0 0.5rem;
    white-space: pre;
  }

  .diff-prefix {
    display: inline-block;
    width: 1rem;
    color: var(--text-muted, #6c7086);
  }

  .diff-row.add .diff-prefix { color: var(--success-color, #a6e3a1); }
  .diff-row.del .diff-prefix { color: var(--error-color, #f38ba8); }

  .diff-text {
    color: var(--text-color, #cdd6f4);
  }
</style>
