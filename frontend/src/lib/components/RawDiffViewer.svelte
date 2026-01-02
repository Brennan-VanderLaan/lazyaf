<script lang="ts">
  /**
   * RawDiffViewer - Renders a raw git diff string with syntax highlighting
   * Used for playground results where we have diff text directly
   */
  export let diff: string;
  export let filesChanged: string[] = [];

  interface ParsedFile {
    path: string;
    status: 'added' | 'deleted' | 'modified';
    additions: number;
    deletions: number;
    lines: Array<{type: string; content: string; oldNum?: number; newNum?: number}>;
  }

  let expandedFiles: Set<string> = new Set();

  $: parsedFiles = parseDiff(diff);

  // Auto-expand first few files when diff changes
  $: if (parsedFiles.length > 0) {
    expandedFiles = new Set(parsedFiles.slice(0, 3).map(f => f.path));
  }

  function parseDiff(diffText: string): ParsedFile[] {
    if (!diffText) return [];

    const files: ParsedFile[] = [];
    const lines = diffText.split('\n');
    let currentFile: ParsedFile | null = null;
    let oldLine = 0;
    let newLine = 0;

    for (const line of lines) {
      // New file diff header
      if (line.startsWith('diff --git')) {
        if (currentFile) {
          files.push(currentFile);
        }
        // Extract file path from "diff --git a/path b/path"
        const match = line.match(/diff --git a\/(.*) b\/(.*)/);
        const path = match ? match[2] : 'unknown';
        currentFile = {
          path,
          status: 'modified',
          additions: 0,
          deletions: 0,
          lines: []
        };
        continue;
      }

      if (!currentFile) {
        // Handle diff without proper git headers (just raw unified diff)
        if (line.startsWith('@@') || line.startsWith('+') || line.startsWith('-')) {
          currentFile = {
            path: filesChanged[0] || 'changes',
            status: 'modified',
            additions: 0,
            deletions: 0,
            lines: []
          };
        } else {
          continue;
        }
      }

      // File status indicators
      if (line.startsWith('new file')) {
        currentFile.status = 'added';
        continue;
      }
      if (line.startsWith('deleted file')) {
        currentFile.status = 'deleted';
        continue;
      }
      if (line.startsWith('index ') || line.startsWith('---') || line.startsWith('+++')) {
        continue;
      }

      // Hunk header
      if (line.startsWith('@@')) {
        const match = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        if (match) {
          oldLine = parseInt(match[1]) - 1;
          newLine = parseInt(match[2]) - 1;
        }
        currentFile.lines.push({ type: 'hunk', content: line });
        continue;
      }

      // Diff content
      if (line.startsWith('+')) {
        newLine++;
        currentFile.additions++;
        currentFile.lines.push({ type: 'add', content: line.slice(1), newNum: newLine });
      } else if (line.startsWith('-')) {
        oldLine++;
        currentFile.deletions++;
        currentFile.lines.push({ type: 'del', content: line.slice(1), oldNum: oldLine });
      } else if (line.startsWith(' ') || line === '') {
        oldLine++;
        newLine++;
        currentFile.lines.push({ type: 'ctx', content: line.slice(1) || '', oldNum: oldLine, newNum: newLine });
      }
    }

    if (currentFile) {
      files.push(currentFile);
    }

    return files;
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

  $: totalAdditions = parsedFiles.reduce((sum, f) => sum + f.additions, 0);
  $: totalDeletions = parsedFiles.reduce((sum, f) => sum + f.deletions, 0);
</script>

<div class="diff-viewer">
  <div class="diff-header">
    <div class="diff-summary">
      <span class="file-count">{parsedFiles.length} file{parsedFiles.length !== 1 ? 's' : ''} changed</span>
      <span class="stats">
        <span class="additions">+{totalAdditions}</span>
        <span class="deletions">-{totalDeletions}</span>
      </span>
    </div>
  </div>

  {#if parsedFiles.length === 0}
    <div class="no-changes">No changes to display.</div>
  {:else}
    <div class="file-list">
      {#each parsedFiles as file}
        <div class="file-item">
          <button
            type="button"
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
              {#if file.status === 'deleted' && file.lines.length === 0}
                <div class="file-deleted-notice">File was deleted</div>
              {:else if file.lines.length === 0}
                <div class="no-diff">No diff content available</div>
              {:else}
                <table class="diff-table">
                  <tbody>
                    {#each file.lines as line}
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

<style>
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

  .file-count {
    color: var(--text-color, #cdd6f4);
    font-size: 0.9rem;
  }

  .stats {
    display: flex;
    gap: 0.75rem;
    font-size: 0.85rem;
  }

  .additions {
    color: var(--success-color, #a6e3a1);
  }

  .deletions {
    color: var(--error-color, #f38ba8);
  }

  .no-changes {
    padding: 2rem;
    text-align: center;
    color: var(--text-muted, #6c7086);
  }

  .file-list {
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
    white-space: pre-wrap;
    word-break: break-all;
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
