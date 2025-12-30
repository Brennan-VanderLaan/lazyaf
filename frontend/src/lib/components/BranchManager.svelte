<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { repos } from '../api/client';
  import type { BranchInfo } from '../api/client';

  export let repoId: string;
  export let repoName: string;
  export let cloneUrl: string = '';

  const dispatch = createEventDispatcher<{ close: void; updated: void }>();

  let branches: BranchInfo[] = [];
  let orphanedCount = 0;
  let damagedCount = 0;
  let defaultBranch = '';
  let remoteUrl: string | null = null;
  let loading = true;
  let error = '';
  let actionInProgress = '';
  let showConfirmCleanup = false;
  let showConfirmCleanupDamaged = false;
  let showConfirmSync = false;
  let showConfirmReinit = false;
  let syncResult: { success: boolean; message: string; deleted_branches?: string[] } | null = null;
  let expandedBranch: string | null = null;  // Which branch's details are expanded
  let copied = false;

  $: if (repoId) {
    loadBranches(true);  // Always verify on load
  }

  async function loadBranches(verify: boolean = true) {
    loading = true;
    error = '';
    try {
      const response = await repos.branchesInfo(repoId, verify);
      branches = response.branches;
      orphanedCount = response.orphaned_count;
      damagedCount = response.damaged_count;
      defaultBranch = response.default_branch;
      remoteUrl = response.remote_url;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load branches';
    } finally {
      loading = false;
    }
  }

  async function copyToClipboard(text: string) {
    await navigator.clipboard.writeText(text);
    copied = true;
    setTimeout(() => copied = false, 2000);
  }

  function toggleExpand(branchName: string) {
    expandedBranch = expandedBranch === branchName ? null : branchName;
  }

  async function deleteBranch(branchName: string) {
    if (branchName === defaultBranch) {
      error = 'Cannot delete the default branch';
      return;
    }

    actionInProgress = `delete:${branchName}`;
    error = '';
    try {
      await repos.deleteBranch(repoId, branchName);
      await loadBranches();
      dispatch('updated');
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to delete branch';
    } finally {
      actionInProgress = '';
    }
  }

  async function cleanupOrphans() {
    showConfirmCleanup = false;
    actionInProgress = 'cleanup';
    error = '';
    try {
      const result = await repos.cleanupOrphans(repoId);
      if (result.deleted_branches.length > 0) {
        syncResult = {
          success: true,
          message: `Deleted ${result.deleted_branches.length} orphaned branch(es)`,
          deleted_branches: result.deleted_branches,
        };
      } else {
        syncResult = {
          success: true,
          message: 'No orphaned branches to clean up',
        };
      }
      await loadBranches();
      dispatch('updated');
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to cleanup branches';
    } finally {
      actionInProgress = '';
    }
  }

  async function cleanupDamaged() {
    showConfirmCleanupDamaged = false;
    actionInProgress = 'cleanup-damaged';
    error = '';
    const damagedBranches = branches.filter(b => b.is_damaged && !b.is_default);
    const deleted: string[] = [];
    const errors: string[] = [];

    for (const branch of damagedBranches) {
      try {
        await repos.deleteBranch(repoId, branch.name);
        deleted.push(branch.name);
      } catch (e) {
        errors.push(`${branch.name}: ${e instanceof Error ? e.message : 'unknown error'}`);
      }
    }

    if (deleted.length > 0) {
      syncResult = {
        success: errors.length === 0,
        message: `Deleted ${deleted.length} damaged branch(es)${errors.length > 0 ? ` (${errors.length} failed)` : ''}`,
        deleted_branches: deleted,
      };
    } else if (errors.length > 0) {
      error = `Failed to delete damaged branches: ${errors.join(', ')}`;
    }

    await loadBranches();
    dispatch('updated');
    actionInProgress = '';
  }

  async function syncFromDisk() {
    showConfirmSync = false;
    actionInProgress = 'sync';
    error = '';
    try {
      const result = await repos.sync(repoId);
      syncResult = {
        success: result.success,
        message: result.message,
        deleted_branches: result.cleanup?.deleted_branches,
      };
      await loadBranches();
      dispatch('updated');
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to sync repo';
    } finally {
      actionInProgress = '';
    }
  }

  async function reinitializeRepo() {
    showConfirmReinit = false;
    actionInProgress = 'reinit';
    error = '';
    try {
      const result = await repos.reinitialize(repoId);
      syncResult = {
        success: result.success,
        message: result.message,
      };
      await loadBranches();
      dispatch('updated');
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to reinitialize repo';
    } finally {
      actionInProgress = '';
    }
  }

  function formatTime(timestamp: number | null): string {
    if (!timestamp) return 'Unknown';
    const date = new Date(timestamp * 1000);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      dispatch('close');
    }
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="modal-backdrop" on:click={() => dispatch('close')} on:keydown={(e) => e.key === 'Enter' && dispatch('close')} role="button" tabindex="0">
  <div class="modal" on:click|stopPropagation role="dialog" aria-modal="true">
    <div class="modal-header">
      <h2>Branch Manager</h2>
      <span class="repo-badge">{repoName}</span>
      <button type="button" class="btn-close" on:click={() => dispatch('close')}>x</button>
    </div>

    <div class="modal-body">
      {#if error}
        <div class="error-banner">
          <span>{error}</span>
          <button type="button" class="btn-dismiss" on:click={() => error = ''}>x</button>
        </div>
      {/if}

      {#if syncResult}
        <div class="result-banner" class:success={syncResult.success}>
          <div class="result-message">{syncResult.message}</div>
          {#if syncResult.deleted_branches && syncResult.deleted_branches.length > 0}
            <div class="deleted-list">
              <strong>Deleted:</strong>
              {#each syncResult.deleted_branches as branch}
                <code>{branch}</code>
              {/each}
            </div>
          {/if}
          <button type="button" class="btn-dismiss" on:click={() => syncResult = null}>x</button>
        </div>
      {/if}

      {#if branches.some(b => b.is_damaged)}
        {@const defaultBranchDamaged = branches.find(b => b.is_default)?.is_damaged}
        <div class="integrity-warning" class:critical={defaultBranchDamaged}>
          <strong>Repository integrity issues detected</strong>
          {#if defaultBranchDamaged}
            <p class="critical-note">The default branch ({defaultBranch}) is damaged and cannot be deleted.</p>
          {/if}
          <p>Some branches have missing git objects (corrupted pack files or incomplete pushes). These branches cannot be cloned or checked out.</p>

          <div class="repair-section">
            <p><strong>Repair options:</strong></p>

            <div class="repair-option">
              <span class="repair-label">1. Push from your local checkout to restore missing objects:</span>
              <div class="cmd-group">
                <code>git push {cloneUrl || `<clone-url>`} --all --force</code>
                <button type="button" class="btn-copy-sm" on:click={() => copyToClipboard(`git push ${cloneUrl || '<clone-url>'} --all --force`)}>
                  {copied ? '!' : '+'}
                </button>
              </div>
            </div>

            {#if remoteUrl}
              <div class="repair-option">
                <span class="repair-label">2. Or fetch from upstream remote and push to LazyAF:</span>
                <div class="cmd-group">
                  <code>git fetch origin && git push {cloneUrl || `<clone-url>`} --all --force</code>
                  <button type="button" class="btn-copy-sm" on:click={() => copyToClipboard(`git fetch origin && git push ${cloneUrl || '<clone-url>'} --all --force`)}>
                    {copied ? '!' : '+'}
                  </button>
                </div>
                <span class="cmd-note">Remote: {remoteUrl}</span>
              </div>
            {/if}

            <div class="repair-option">
              <span class="repair-label">{remoteUrl ? '3' : '2'}. Delete the damaged branches (if not the default branch)</span>
            </div>

            {#if defaultBranchDamaged}
              <div class="repair-option nuclear">
                <span class="repair-label">{remoteUrl ? '4' : '3'}. Nuclear option: Reinitialize the repository</span>
                <p class="nuclear-warning">This deletes ALL branches and objects. You must push your local repo again afterwards.</p>
                <button
                  type="button"
                  class="btn btn-danger"
                  on:click={() => showConfirmReinit = true}
                  disabled={actionInProgress !== ''}
                >
                  {actionInProgress === 'reinit' ? 'Reinitializing...' : 'Reinitialize Repository'}
                </button>
              </div>
            {/if}
          </div>

          <p class="note">Click on a damaged branch row to see which objects are missing.</p>
        </div>
      {/if}

      <div class="actions-bar">
        <div class="stats">
          <span class="stat">{branches.length} branches</span>
          {#if orphanedCount > 0}
            <span class="stat warning">{orphanedCount} orphaned</span>
          {/if}
          {#if damagedCount > 0}
            <span class="stat danger">{damagedCount} damaged</span>
          {/if}
        </div>
        <div class="action-buttons">
          {#if orphanedCount > 0}
            <button
              type="button"
              class="btn btn-warning"
              on:click={() => showConfirmCleanup = true}
              disabled={actionInProgress !== ''}
            >
              {actionInProgress === 'cleanup' ? 'Cleaning...' : `Cleanup ${orphanedCount} Orphaned`}
            </button>
          {/if}
          {#if branches.some(b => b.is_damaged && !b.is_default)}
            <button
              type="button"
              class="btn btn-danger"
              on:click={() => showConfirmCleanupDamaged = true}
              disabled={actionInProgress !== ''}
            >
              {actionInProgress === 'cleanup-damaged' ? 'Cleaning...' : `Delete ${branches.filter(b => b.is_damaged && !b.is_default).length} Damaged`}
            </button>
          {/if}
          <button
            type="button"
            class="btn btn-secondary"
            on:click={() => showConfirmSync = true}
            disabled={actionInProgress !== ''}
          >
            {actionInProgress === 'sync' ? 'Syncing...' : 'Sync from Disk'}
          </button>
        </div>
      </div>

      {#if showConfirmCleanup}
        <div class="confirm-dialog">
          <p>Are you sure you want to delete all {orphanedCount} orphaned branches?</p>
          <p class="muted">Orphaned branches point to commits that no longer exist.</p>
          <div class="confirm-buttons">
            <button type="button" class="btn btn-secondary" on:click={() => showConfirmCleanup = false}>Cancel</button>
            <button type="button" class="btn btn-warning" on:click={cleanupOrphans}>Delete Orphaned Branches</button>
          </div>
        </div>
      {/if}

      {#if showConfirmCleanupDamaged}
        <div class="confirm-dialog danger">
          <p><strong>Delete all damaged branches?</strong></p>
          <p class="muted">This will delete {branches.filter(b => b.is_damaged && !b.is_default).length} branch(es) with missing git objects. The default branch is protected and will not be deleted.</p>
          <div class="damaged-list">
            {#each branches.filter(b => b.is_damaged && !b.is_default) as branch}
              <code>{branch.name}</code>
            {/each}
          </div>
          <div class="confirm-buttons">
            <button type="button" class="btn btn-secondary" on:click={() => showConfirmCleanupDamaged = false}>Cancel</button>
            <button type="button" class="btn btn-danger" on:click={cleanupDamaged}>Delete Damaged Branches</button>
          </div>
        </div>
      {/if}

      {#if showConfirmSync}
        <div class="confirm-dialog">
          <p><strong>Sync from Disk</strong></p>
          <p class="muted">This re-reads all refs from the git directory, removes orphaned branches, and verifies object integrity. Use this to diagnose corrupted or inconsistent repo state.</p>
          <div class="confirm-buttons">
            <button type="button" class="btn btn-secondary" on:click={() => showConfirmSync = false}>Cancel</button>
            <button type="button" class="btn btn-primary" on:click={syncFromDisk}>Sync Now</button>
          </div>
        </div>
      {/if}

      {#if showConfirmReinit}
        <div class="confirm-dialog danger nuclear-confirm">
          <p><strong>Reinitialize Repository?</strong></p>
          <p class="danger-text">This will permanently delete ALL branches and ALL git objects in this repository.</p>
          <p class="muted">After reinitializing, you must push your local repo to restore it:</p>
          <div class="cmd-group">
            <code>git push {cloneUrl || `<clone-url>`} --all</code>
          </div>
          <p class="danger-text">This action cannot be undone!</p>
          <div class="confirm-buttons">
            <button type="button" class="btn btn-secondary" on:click={() => showConfirmReinit = false}>Cancel</button>
            <button type="button" class="btn btn-danger" on:click={reinitializeRepo}>
              Yes, Reinitialize Repository
            </button>
          </div>
        </div>
      {/if}

      {#if loading}
        <div class="loading">Loading branches...</div>
      {:else if branches.length === 0}
        <div class="empty">No branches found. Push your repo to get started.</div>
      {:else}
        <div class="branch-table">
          <div class="table-header">
            <div class="col-branch">Branch</div>
            <div class="col-sha">SHA</div>
            <div class="col-time">Last Commit</div>
            <div class="col-status">Status</div>
            <div class="col-actions">Actions</div>
          </div>
          {#each branches as branch}
            <div
              class="table-row"
              class:orphaned={branch.is_orphaned}
              class:damaged={branch.is_damaged}
              class:default={branch.is_default}
              class:expandable={branch.is_damaged}
              class:expanded={expandedBranch === branch.name}
              on:click={() => branch.is_damaged && toggleExpand(branch.name)}
              on:keydown={(e) => e.key === 'Enter' && branch.is_damaged && toggleExpand(branch.name)}
              role={branch.is_damaged ? 'button' : undefined}
              tabindex={branch.is_damaged ? 0 : undefined}
            >
              <div class="col-branch">
                <span class="branch-name">{branch.name}</span>
                {#if branch.is_default}
                  <span class="badge badge-default">default</span>
                {/if}
                {#if branch.name.startsWith('lazyaf/')}
                  <span class="badge badge-agent">agent</span>
                {/if}
              </div>
              <div class="col-sha">
                <code>{branch.short_sha || branch.sha.slice(0, 7)}</code>
              </div>
              <div class="col-time">
                {#if branch.is_orphaned}
                  <span class="orphan-note">No commit</span>
                {:else if branch.is_damaged}
                  <span class="damaged-note">{branch.missing_objects?.length || '?'} missing obj</span>
                {:else}
                  {formatTime(branch.commit_time)}
                {/if}
              </div>
              <div class="col-status">
                {#if branch.is_orphaned}
                  <span class="status-badge danger">Orphaned</span>
                {:else if branch.is_damaged}
                  <span class="status-badge danger">Damaged</span>
                {:else if branch.is_default}
                  <span class="status-badge primary">Protected</span>
                {:else}
                  <span class="status-badge">OK</span>
                {/if}
              </div>
              <div class="col-actions">
                {#if branch.is_default}
                  <span class="protected-note">Protected</span>
                {:else}
                  <button
                    type="button"
                    class="btn btn-sm btn-danger"
                    on:click|stopPropagation={() => deleteBranch(branch.name)}
                    disabled={actionInProgress !== ''}
                    title="Delete branch"
                  >
                    {actionInProgress === `delete:${branch.name}` ? '...' : 'Delete'}
                  </button>
                {/if}
              </div>
            </div>
            {#if branch.is_damaged && expandedBranch === branch.name}
              <div class="branch-details">
                <div class="details-header">
                  <strong>Missing Objects ({branch.missing_objects?.length || 0})</strong>
                  <span class="muted">Checked {branch.objects_checked || 0} objects</span>
                </div>
                {#if branch.missing_objects && branch.missing_objects.length > 0}
                  <div class="missing-objects-list">
                    {#each branch.missing_objects.slice(0, 20) as sha}
                      <code>{sha.slice(0, 12)}</code>
                    {/each}
                    {#if branch.missing_objects.length > 20}
                      <span class="more-objects">+{branch.missing_objects.length - 20} more</span>
                    {/if}
                  </div>
                {:else}
                  <p class="muted">No missing objects recorded</p>
                {/if}
              </div>
            {/if}
          {/each}
        </div>
      {/if}
    </div>

    <div class="modal-footer">
      <button type="button" class="btn btn-secondary" on:click={() => dispatch('close')}>Close</button>
      <button type="button" class="btn btn-primary" on:click={loadBranches} disabled={loading}>
        {loading ? 'Loading...' : 'Refresh'}
      </button>
    </div>
  </div>
</div>

<style>
  .modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: var(--surface-color, #1e1e2e);
    border-radius: 12px;
    width: min(900px, 95vw);
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  }

  .modal-header {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 1rem 1.5rem;
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.2rem;
    color: var(--text-color, #cdd6f4);
  }

  .repo-badge {
    font-size: 0.8rem;
    padding: 0.25rem 0.5rem;
    background: var(--surface-alt, #181825);
    border-radius: 4px;
    color: var(--text-muted, #6c7086);
  }

  .btn-close {
    margin-left: auto;
    background: none;
    border: none;
    color: var(--text-muted, #6c7086);
    font-size: 1.2rem;
    cursor: pointer;
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
  }

  .btn-close:hover {
    background: var(--hover-color, #313244);
    color: var(--text-color, #cdd6f4);
  }

  .modal-body {
    flex: 1;
    overflow-y: auto;
    padding: 1rem 1.5rem;
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
    padding: 1rem 1.5rem;
    border-top: 1px solid var(--border-color, #45475a);
  }

  .error-banner {
    background: rgba(243, 139, 168, 0.15);
    border: 1px solid var(--error-color, #f38ba8);
    border-radius: 6px;
    padding: 0.75rem 1rem;
    margin-bottom: 1rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    color: var(--error-color, #f38ba8);
  }

  .result-banner {
    background: rgba(166, 227, 161, 0.15);
    border: 1px solid var(--success-color, #a6e3a1);
    border-radius: 6px;
    padding: 0.75rem 1rem;
    margin-bottom: 1rem;
    color: var(--success-color, #a6e3a1);
  }

  .result-banner.success {
    background: rgba(166, 227, 161, 0.15);
    border-color: var(--success-color, #a6e3a1);
    color: var(--success-color, #a6e3a1);
  }

  .result-message {
    font-weight: 500;
    margin-bottom: 0.5rem;
  }

  .deleted-list {
    font-size: 0.85rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }

  .deleted-list code {
    background: rgba(0, 0, 0, 0.2);
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    font-size: 0.8rem;
  }

  .btn-dismiss {
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    font-size: 1rem;
    padding: 0.25rem;
    opacity: 0.7;
  }

  .btn-dismiss:hover {
    opacity: 1;
  }

  .actions-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
    padding: 0.75rem;
    background: var(--surface-alt, #181825);
    border-radius: 6px;
  }

  .stats {
    display: flex;
    gap: 1rem;
  }

  .stat {
    font-size: 0.9rem;
    color: var(--text-color, #cdd6f4);
  }

  .stat.warning {
    color: var(--warning-color, #f9e2af);
  }

  .stat.danger {
    color: var(--error-color, #f38ba8);
  }

  .integrity-warning {
    background: rgba(243, 139, 168, 0.1);
    border: 1px solid var(--error-color, #f38ba8);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1rem;
    color: var(--text-color, #cdd6f4);
  }

  .integrity-warning.critical {
    background: rgba(243, 139, 168, 0.15);
    border-width: 2px;
  }

  .integrity-warning strong {
    color: var(--error-color, #f38ba8);
  }

  .critical-note {
    background: rgba(243, 139, 168, 0.2);
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    margin: 0.5rem 0;
    font-weight: 500;
    color: var(--error-color, #f38ba8);
  }

  .integrity-warning p {
    margin: 0.5rem 0;
    font-size: 0.9rem;
  }

  .integrity-warning ul {
    margin: 0.5rem 0;
    padding-left: 1.5rem;
    font-size: 0.85rem;
  }

  .integrity-warning li {
    margin: 0.25rem 0;
  }

  .integrity-warning .note {
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
    margin-top: 0.75rem;
    font-style: italic;
  }

  .repair-section {
    margin-top: 1rem;
  }

  .repair-option {
    margin: 0.75rem 0;
    padding-left: 0.5rem;
    border-left: 2px solid var(--border-color, #45475a);
  }

  .repair-option.nuclear {
    border-left-color: var(--error-color, #f38ba8);
    background: rgba(243, 139, 168, 0.05);
    padding: 0.75rem;
    border-radius: 0 4px 4px 0;
    margin-top: 1rem;
  }

  .nuclear-warning {
    font-size: 0.8rem;
    color: var(--error-color, #f38ba8);
    margin: 0.5rem 0;
  }

  .nuclear-confirm {
    border-color: var(--error-color, #f38ba8);
    border-width: 2px;
  }

  .danger-text {
    color: var(--error-color, #f38ba8);
    font-weight: 500;
  }

  .repair-label {
    font-size: 0.85rem;
    display: block;
    margin-bottom: 0.5rem;
  }

  .cmd-group {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.25rem;
  }

  .cmd-group code {
    flex: 1;
    padding: 0.5rem 0.75rem;
    background: var(--surface-alt, #181825);
    border-radius: 4px;
    font-size: 0.8rem;
    color: var(--text-color, #cdd6f4);
    font-family: monospace;
    overflow-x: auto;
  }

  .cmd-note {
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
  }

  .btn-copy-sm {
    padding: 0.35rem 0.5rem;
    background: var(--surface-color, #1e1e2e);
    color: var(--text-muted, #6c7086);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
    font-weight: 600;
  }

  .btn-copy-sm:hover {
    background: var(--primary-color, #89b4fa);
    color: #1e1e2e;
    border-color: var(--primary-color, #89b4fa);
  }

  .action-buttons {
    display: flex;
    gap: 0.5rem;
  }

  .confirm-dialog {
    background: var(--surface-alt, #181825);
    border: 1px solid var(--warning-color, #f9e2af);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1rem;
  }

  .confirm-dialog.danger {
    border-color: var(--error-color, #f38ba8);
  }

  .confirm-dialog p {
    margin: 0 0 0.5rem 0;
    color: var(--text-color, #cdd6f4);
  }

  .confirm-dialog .muted {
    color: var(--text-muted, #6c7086);
    font-size: 0.85rem;
  }

  .confirm-buttons {
    display: flex;
    gap: 0.5rem;
    margin-top: 1rem;
  }

  .damaged-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin: 0.75rem 0;
    max-height: 100px;
    overflow-y: auto;
  }

  .damaged-list code {
    background: rgba(243, 139, 168, 0.2);
    padding: 0.2rem 0.5rem;
    border-radius: 3px;
    font-size: 0.8rem;
    color: var(--error-color, #f38ba8);
  }

  .btn {
    padding: 0.5rem 1rem;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 500;
    font-size: 0.9rem;
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-primary {
    background: var(--primary-color, #89b4fa);
    color: #1e1e2e;
  }

  .btn-secondary {
    background: var(--surface-color, #313244);
    color: var(--text-color, #cdd6f4);
    border: 1px solid var(--border-color, #45475a);
  }

  .btn-warning {
    background: var(--warning-color, #f9e2af);
    color: #1e1e2e;
  }

  .btn-danger {
    background: var(--error-color, #f38ba8);
    color: #1e1e2e;
  }

  .btn-sm {
    padding: 0.3rem 0.6rem;
    font-size: 0.8rem;
  }

  .loading, .empty {
    text-align: center;
    padding: 2rem;
    color: var(--text-muted, #6c7086);
  }

  .branch-table {
    border: 1px solid var(--border-color, #45475a);
    border-radius: 8px;
    overflow: hidden;
  }

  .table-header {
    display: grid;
    grid-template-columns: 2fr 1fr 1fr 1fr 100px;
    gap: 1rem;
    padding: 0.75rem 1rem;
    background: var(--surface-alt, #181825);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-muted, #6c7086);
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .table-row {
    display: grid;
    grid-template-columns: 2fr 1fr 1fr 1fr 100px;
    gap: 1rem;
    padding: 0.75rem 1rem;
    align-items: center;
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .table-row:last-child {
    border-bottom: none;
  }

  .table-row:hover {
    background: var(--hover-color, #313244);
  }

  .table-row.orphaned {
    background: rgba(243, 139, 168, 0.05);
  }

  .table-row.damaged {
    background: rgba(243, 139, 168, 0.08);
  }

  .table-row.expandable {
    cursor: pointer;
  }

  .table-row.expandable:hover {
    background: rgba(243, 139, 168, 0.15);
  }

  .table-row.expanded {
    background: rgba(243, 139, 168, 0.12);
    border-bottom: none;
  }

  .table-row.default {
    background: rgba(137, 180, 250, 0.05);
  }

  .branch-details {
    padding: 1rem;
    background: rgba(243, 139, 168, 0.05);
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .details-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
  }

  .details-header strong {
    color: var(--error-color, #f38ba8);
    font-size: 0.9rem;
  }

  .details-header .muted {
    font-size: 0.8rem;
    color: var(--text-muted, #6c7086);
  }

  .missing-objects-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    max-height: 150px;
    overflow-y: auto;
    padding: 0.5rem;
    background: var(--surface-alt, #181825);
    border-radius: 4px;
  }

  .missing-objects-list code {
    font-family: monospace;
    font-size: 0.75rem;
    background: rgba(243, 139, 168, 0.15);
    padding: 0.2rem 0.4rem;
    border-radius: 3px;
    color: var(--error-color, #f38ba8);
  }

  .more-objects {
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
    font-style: italic;
    padding: 0.2rem 0.4rem;
  }

  .col-branch {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .branch-name {
    color: var(--text-color, #cdd6f4);
    font-weight: 500;
  }

  .badge {
    font-size: 0.65rem;
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .badge-default {
    background: var(--primary-color, #89b4fa);
    color: #1e1e2e;
  }

  .badge-agent {
    background: var(--accent-color, #cba6f7);
    color: #1e1e2e;
  }

  .col-sha code {
    font-family: monospace;
    font-size: 0.85rem;
    color: var(--primary-color, #89b4fa);
    background: var(--surface-alt, #181825);
    padding: 0.2rem 0.4rem;
    border-radius: 3px;
  }

  .col-time {
    font-size: 0.85rem;
    color: var(--text-muted, #6c7086);
  }

  .orphan-note {
    color: var(--error-color, #f38ba8);
    font-style: italic;
  }

  .damaged-note {
    color: var(--error-color, #f38ba8);
    font-style: italic;
    font-size: 0.8rem;
  }

  .status-badge {
    font-size: 0.75rem;
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    background: var(--surface-alt, #181825);
    color: var(--text-muted, #6c7086);
  }

  .status-badge.primary {
    background: rgba(137, 180, 250, 0.2);
    color: var(--primary-color, #89b4fa);
  }

  .status-badge.danger {
    background: rgba(243, 139, 168, 0.2);
    color: var(--error-color, #f38ba8);
  }

  .col-actions {
    text-align: right;
  }

  .protected-note {
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  /* Responsive */
  @media (max-width: 700px) {
    .table-header {
      display: none;
    }

    .table-row {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      padding: 1rem;
    }

    .col-branch {
      font-size: 1rem;
    }

    .col-actions {
      text-align: left;
    }
  }
</style>
