<script lang="ts">
  import type { ConflictDetail } from '../api/types';

  export let conflicts: ConflictDetail[];
  export let onResolve: (resolutions: Array<{ path: string; content: string }>) => void;
  export let onCancel: () => void;

  interface Resolution {
    path: string;
    content: string;
    choice: 'ours' | 'theirs' | 'custom';
  }

  let resolutions: Map<string, Resolution> = new Map();
  let expandedFiles: Set<string> = new Set();

  // Initialize with first file expanded
  if (conflicts.length > 0) {
    expandedFiles.add(conflicts[0].path);
  }

  function toggleFile(path: string) {
    if (expandedFiles.has(path)) {
      expandedFiles.delete(path);
    } else {
      expandedFiles.add(path);
    }
    expandedFiles = expandedFiles;
  }

  function chooseVersion(path: string, choice: 'ours' | 'theirs') {
    const conflict = conflicts.find(c => c.path === path);
    if (!conflict) return;

    const content = choice === 'ours' ? conflict.ours_content : conflict.theirs_content;
    if (content === null) return;

    resolutions.set(path, { path, content, choice });
    resolutions = resolutions;
  }

  function setCustomContent(path: string, content: string) {
    resolutions.set(path, { path, content, choice: 'custom' });
    resolutions = resolutions;
  }

  function handleResolve() {
    // Check all conflicts are resolved
    const unresolvedFiles = conflicts.filter(c => !resolutions.has(c.path));
    if (unresolvedFiles.length > 0) {
      alert(`Please resolve all conflicts. Unresolved files: ${unresolvedFiles.map(f => f.path).join(', ')}`);
      return;
    }

    const resolutionList = Array.from(resolutions.values()).map(r => ({
      path: r.path,
      content: r.content
    }));

    onResolve(resolutionList);
  }

  function getResolution(path: string): Resolution | undefined {
    return resolutions.get(path);
  }
</script>

<div class="conflict-resolver">
  <div class="conflict-header">
    <h3>Resolve Merge Conflicts</h3>
    <p class="conflict-count">{conflicts.length} file(s) with conflicts</p>
  </div>

  <div class="conflict-files">
    {#each conflicts as conflict}
      <div class="conflict-file">
        <button
          class="file-header"
          on:click={() => toggleFile(conflict.path)}
        >
          <span class="expand-icon">{expandedFiles.has(conflict.path) ? '▼' : '▶'}</span>
          <span class="file-path">{conflict.path}</span>
          {#if getResolution(conflict.path)}
            <span class="resolved-badge">✓ Resolved</span>
          {/if}
        </button>

        {#if expandedFiles.has(conflict.path)}
          <div class="conflict-content">
            <div class="conflict-actions">
              <button
                class="action-btn"
                class:active={getResolution(conflict.path)?.choice === 'ours'}
                on:click={() => chooseVersion(conflict.path, 'ours')}
                disabled={conflict.ours_content === null}
              >
                Use Target Branch Version
              </button>
              <button
                class="action-btn"
                class:active={getResolution(conflict.path)?.choice === 'theirs'}
                on:click={() => chooseVersion(conflict.path, 'theirs')}
                disabled={conflict.theirs_content === null}
              >
                Use Source Branch Version
              </button>
            </div>

            <div class="versions">
              <div class="version">
                <h4>Target Branch (Ours)</h4>
                <pre class="code-block">{conflict.ours_content || '(file deleted)'}</pre>
              </div>
              <div class="version">
                <h4>Source Branch (Theirs)</h4>
                <pre class="code-block">{conflict.theirs_content || '(file deleted)'}</pre>
              </div>
            </div>

            <div class="custom-edit">
              <h4>Or Edit Manually:</h4>
              <textarea
                class="custom-editor"
                value={getResolution(conflict.path)?.content || conflict.theirs_content || conflict.ours_content || ''}
                on:input={(e) => setCustomContent(conflict.path, e.currentTarget.value)}
                placeholder="Edit the merged content here..."
              ></textarea>
            </div>
          </div>
        {/if}
      </div>
    {/each}
  </div>

  <div class="conflict-footer">
    <button class="btn-cancel" on:click={onCancel}>Cancel</button>
    <button class="btn-resolve" on:click={handleResolve}>Complete Merge</button>
  </div>
</div>

<style>
  .conflict-resolver {
    background: #1e1e1e;
    border: 1px solid #333;
    border-radius: 4px;
    margin: 1rem 0;
    overflow: hidden;
  }

  .conflict-header {
    padding: 1rem;
    border-bottom: 1px solid #333;
    background: #252525;
  }

  .conflict-header h3 {
    margin: 0 0 0.5rem 0;
    color: #ff6b6b;
    font-size: 1.1rem;
  }

  .conflict-count {
    margin: 0;
    color: #999;
    font-size: 0.9rem;
  }

  .conflict-files {
    max-height: 600px;
    overflow-y: auto;
  }

  .conflict-file {
    border-bottom: 1px solid #333;
  }

  .conflict-file:last-child {
    border-bottom: none;
  }

  .file-header {
    width: 100%;
    padding: 0.75rem 1rem;
    background: #2a2a2a;
    border: none;
    color: #fff;
    text-align: left;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    transition: background 0.2s;
  }

  .file-header:hover {
    background: #333;
  }

  .expand-icon {
    color: #999;
    font-size: 0.8rem;
  }

  .file-path {
    flex: 1;
    font-family: 'Monaco', 'Courier New', monospace;
    font-size: 0.9rem;
  }

  .resolved-badge {
    background: #2d5a3c;
    color: #6bc98e;
    padding: 0.2rem 0.6rem;
    border-radius: 3px;
    font-size: 0.8rem;
  }

  .conflict-content {
    padding: 1rem;
    background: #1e1e1e;
  }

  .conflict-actions {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1rem;
  }

  .action-btn {
    padding: 0.5rem 1rem;
    background: #333;
    border: 1px solid #555;
    color: #fff;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s;
  }

  .action-btn:hover:not(:disabled) {
    background: #444;
    border-color: #666;
  }

  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .action-btn.active {
    background: #2d5a3c;
    border-color: #6bc98e;
    color: #6bc98e;
  }

  .versions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .version h4 {
    margin: 0 0 0.5rem 0;
    color: #999;
    font-size: 0.9rem;
    font-weight: 500;
  }

  .code-block {
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 0.75rem;
    margin: 0;
    font-family: 'Monaco', 'Courier New', monospace;
    font-size: 0.85rem;
    color: #c9d1d9;
    overflow-x: auto;
    max-height: 300px;
    overflow-y: auto;
  }

  .custom-edit h4 {
    margin: 0 0 0.5rem 0;
    color: #999;
    font-size: 0.9rem;
    font-weight: 500;
  }

  .custom-editor {
    width: 100%;
    min-height: 200px;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 0.75rem;
    color: #c9d1d9;
    font-family: 'Monaco', 'Courier New', monospace;
    font-size: 0.85rem;
    resize: vertical;
  }

  .custom-editor:focus {
    outline: none;
    border-color: #58a6ff;
  }

  .conflict-footer {
    padding: 1rem;
    border-top: 1px solid #333;
    background: #252525;
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }

  .btn-cancel {
    padding: 0.6rem 1.2rem;
    background: #333;
    border: 1px solid #555;
    color: #fff;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn-cancel:hover {
    background: #444;
  }

  .btn-resolve {
    padding: 0.6rem 1.2rem;
    background: #238636;
    border: 1px solid #2ea043;
    color: #fff;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn-resolve:hover {
    background: #2ea043;
  }
</style>
