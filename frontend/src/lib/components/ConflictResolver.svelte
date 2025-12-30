<script lang="ts">
  import type { ConflictDetail } from '../api/types';

  export let conflicts: ConflictDetail[];
  export let onResolve: (resolutions: Array<{ path: string; content: string }>) => void;
  export let onCancel: () => void;
  export let operation: 'merge' | 'rebase' = 'merge';
  export let sourceBranch: string = 'feature';  // Your branch
  export let targetBranch: string = 'main';     // Branch you're merging into or rebasing onto

  interface Resolution {
    path: string;
    content: string;
    choice: 'ours' | 'theirs' | 'custom';
  }

  type ViewMode = 'diff' | 'sidebyside' | 'edit';

  let resolutions: Map<string, Resolution> = new Map();
  let expandedFiles: Set<string> = new Set();
  let viewModes: Record<string, ViewMode> = {};  // Changed to object for reactivity
  let showExplanation = true;

  // Refs for synchronized scrolling
  let scrollContainers: Record<string, { ours?: HTMLElement; theirs?: HTMLElement }> = {};

  // Initialize with first file expanded
  if (conflicts.length > 0) {
    expandedFiles.add(conflicts[0].path);
  }

  // Labels differ based on operation type
  // In git rebase: "ours" = the branch you're rebasing ONTO (target), "theirs" = your changes being replayed
  // In git merge: "ours" = your current branch (target), "theirs" = the branch being merged in (source)
  $: oursLabel = operation === 'rebase'
    ? `${targetBranch} (updating from)`
    : `${targetBranch} (current)`;

  $: theirsLabel = operation === 'rebase'
    ? `${sourceBranch} (your changes)`
    : `${sourceBranch} (merging in)`;

  $: operationTitle = operation === 'rebase' ? 'Resolve Rebase Conflicts' : 'Resolve Merge Conflicts';

  $: operationExplanation = operation === 'rebase'
    ? `Updating ${sourceBranch} to include latest changes from ${targetBranch}`
    : `Merging ${sourceBranch} into ${targetBranch}`;

  function getViewMode(path: string): ViewMode {
    return viewModes[path] || 'diff';
  }

  function setViewMode(path: string, mode: ViewMode) {
    viewModes = { ...viewModes, [path]: mode };
  }

  // Synchronized scrolling
  let isScrolling = false;

  function handleScroll(path: string, source: 'ours' | 'theirs', event: Event) {
    if (isScrolling) return;  // Prevent infinite loop

    const target = event.target as HTMLElement;
    const containers = scrollContainers[path];
    if (!containers) return;

    const other = source === 'ours' ? containers.theirs : containers.ours;
    if (other && other !== target) {
      isScrolling = true;
      other.scrollTop = target.scrollTop;
      other.scrollLeft = target.scrollLeft;
      requestAnimationFrame(() => { isScrolling = false; });
    }
  }

  function scrollSync(node: HTMLElement, params: { path: string; side: 'ours' | 'theirs' }) {
    const { path, side } = params;
    if (!scrollContainers[path]) {
      scrollContainers[path] = {};
    }
    scrollContainers[path][side] = node;

    const handler = (e: Event) => handleScroll(path, side, e);
    node.addEventListener('scroll', handler);

    return {
      destroy() {
        node.removeEventListener('scroll', handler);
        if (scrollContainers[path]) {
          delete scrollContainers[path][side];
        }
      }
    };
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

  function getResolutionLabel(path: string): string {
    const res = getResolution(path);
    if (!res) return '';
    if (res.choice === 'ours') return operation === 'rebase' ? `Using ${targetBranch}` : `Using ${targetBranch}`;
    if (res.choice === 'theirs') return operation === 'rebase' ? `Using ${sourceBranch}` : `Using ${sourceBranch}`;
    return 'Custom edit';
  }

  // Simple diff algorithm - compute line-by-line differences
  interface DiffLine {
    type: 'same' | 'add' | 'del';
    lineNum?: number;
    content: string;
  }

  function computeDiff(base: string | null, target: string | null): DiffLine[] {
    if (base === null && target === null) return [];
    if (base === null) {
      return (target || '').split('\n').map((line, i) => ({
        type: 'add' as const,
        lineNum: i + 1,
        content: line
      }));
    }
    if (target === null) {
      return (base || '').split('\n').map((line, i) => ({
        type: 'del' as const,
        lineNum: i + 1,
        content: line
      }));
    }

    const baseLines = base.split('\n');
    const targetLines = target.split('\n');
    const result: DiffLine[] = [];
    const lcs = computeLCS(baseLines, targetLines);

    let baseIdx = 0;
    let targetIdx = 0;
    let lineNum = 1;

    for (const match of lcs) {
      while (baseIdx < match.baseIdx) {
        result.push({ type: 'del', content: baseLines[baseIdx] });
        baseIdx++;
      }
      while (targetIdx < match.targetIdx) {
        result.push({ type: 'add', lineNum: lineNum++, content: targetLines[targetIdx] });
        targetIdx++;
      }
      result.push({ type: 'same', lineNum: lineNum++, content: targetLines[targetIdx] });
      baseIdx++;
      targetIdx++;
    }

    while (baseIdx < baseLines.length) {
      result.push({ type: 'del', content: baseLines[baseIdx] });
      baseIdx++;
    }
    while (targetIdx < targetLines.length) {
      result.push({ type: 'add', lineNum: lineNum++, content: targetLines[targetIdx] });
      targetIdx++;
    }

    return result;
  }

  interface LCSMatch {
    baseIdx: number;
    targetIdx: number;
  }

  function computeLCS(base: string[], target: string[]): LCSMatch[] {
    const m = base.length;
    const n = target.length;
    const dp: number[][] = Array(m + 1).fill(null).map(() => Array(n + 1).fill(0));

    for (let i = 1; i <= m; i++) {
      for (let j = 1; j <= n; j++) {
        if (base[i - 1] === target[j - 1]) {
          dp[i][j] = dp[i - 1][j - 1] + 1;
        } else {
          dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
        }
      }
    }

    const matches: LCSMatch[] = [];
    let i = m, j = n;
    while (i > 0 && j > 0) {
      if (base[i - 1] === target[j - 1]) {
        matches.unshift({ baseIdx: i - 1, targetIdx: j - 1 });
        i--;
        j--;
      } else if (dp[i - 1][j] > dp[i][j - 1]) {
        i--;
      } else {
        j--;
      }
    }

    return matches;
  }

  function computeThreeWayDiff(base: string | null, ours: string | null, theirs: string | null) {
    const oursChanges = computeDiff(base, ours);
    const theirsChanges = computeDiff(base, theirs);
    return { oursChanges, theirsChanges };
  }

  function getEditorContent(conflict: ConflictDetail): string {
    const resolution = getResolution(conflict.path);
    if (resolution) return resolution.content;
    return conflict.theirs_content || conflict.ours_content || '';
  }
</script>

<div class="conflict-resolver">
  <div class="conflict-header">
    <div class="header-main">
      <h3>{operationTitle}</h3>
      <button type="button" class="toggle-explanation" on:click={() => showExplanation = !showExplanation}>
        {showExplanation ? 'Hide' : 'Show'} explanation
      </button>
    </div>
    <p class="operation-summary">{operationExplanation}</p>
    <p class="conflict-count">{conflicts.length} file(s) need manual resolution</p>
  </div>

  {#if showExplanation}
    <div class="explanation-panel">
      {#if operation === 'rebase'}
        <!-- Rebase explanation -->
        <div class="explanation-content">
          <div class="explanation-title">What's happening:</div>
          <p>You're pulling the latest changes from <code>{targetBranch}</code> into your branch <code>{sourceBranch}</code>.</p>

          <div class="git-graph">
            <div class="graph-before">
              <div class="graph-label">Before (branches diverged):</div>
              <pre class="graph-visual">
{targetBranch}:  ●──●──●──● (new commits)
                 │
        (base) ──●
                 │
{sourceBranch}:  ●──●──● (your commits)</pre>
            </div>
            <div class="graph-arrow">→</div>
            <div class="graph-after">
              <div class="graph-label">After (your branch updated):</div>
              <pre class="graph-visual">
{targetBranch}:  ●──●──●──●
                          │
{sourceBranch}:           ●──●──● (your commits, rebased)</pre>
            </div>
          </div>

          <div class="conflict-explanation">
            <strong>The conflict:</strong> Both branches modified the same lines in these files.
            <ul>
              <li><span class="label-ours">{targetBranch}</span> = Changes from the branch you're updating from</li>
              <li><span class="label-theirs">{sourceBranch}</span> = Your changes that will be kept</li>
            </ul>
            <p class="recommendation">
              <strong>Recommendation:</strong> Usually you want to keep your changes (<code>{sourceBranch}</code>)
              but incorporate any important updates from <code>{targetBranch}</code>.
              Use the "Edit" tab to combine both if needed.
            </p>
          </div>
        </div>
      {:else}
        <!-- Merge explanation -->
        <div class="explanation-content">
          <div class="explanation-title">What's happening:</div>
          <p>You're merging <code>{sourceBranch}</code> into <code>{targetBranch}</code>.</p>

          <div class="git-graph">
            <div class="graph-before">
              <div class="graph-label">Before:</div>
              <pre class="graph-visual">
{targetBranch}:  ●──●──● (current)
                 │
        (base) ──●
                 │
{sourceBranch}:  ●──●──● (to merge)</pre>
            </div>
            <div class="graph-arrow">→</div>
            <div class="graph-after">
              <div class="graph-label">After (merge commit):</div>
              <pre class="graph-visual">
{targetBranch}:  ●──●──●──◆ (merge commit)
                 │       ╱
{sourceBranch}:  ●──●──●</pre>
            </div>
          </div>

          <div class="conflict-explanation">
            <strong>The conflict:</strong> Both branches modified the same lines.
            <ul>
              <li><span class="label-ours">{targetBranch}</span> = Current state of the target branch</li>
              <li><span class="label-theirs">{sourceBranch}</span> = Changes being merged in</li>
            </ul>
          </div>
        </div>
      {/if}
    </div>
  {/if}

  <div class="conflict-files">
    {#each conflicts as conflict (conflict.path)}
      {@const diff = computeThreeWayDiff(conflict.base_content, conflict.ours_content, conflict.theirs_content)}
      {@const currentViewMode = viewModes[conflict.path] || 'diff'}
      <div class="conflict-file">
        <button
          type="button"
          class="file-header"
          on:click={() => toggleFile(conflict.path)}
        >
          <span class="expand-icon">{expandedFiles.has(conflict.path) ? '▼' : '▶'}</span>
          <span class="file-path">{conflict.path}</span>
          {#if getResolution(conflict.path)}
            <span class="resolved-badge">✓ {getResolutionLabel(conflict.path)}</span>
          {:else}
            <span class="unresolved-badge">Needs Resolution</span>
          {/if}
        </button>

        {#if expandedFiles.has(conflict.path)}
          <div class="conflict-content">
            <div class="view-tabs">
              <button
                type="button"
                class="view-tab"
                class:active={currentViewMode === 'diff'}
                on:click={() => setViewMode(conflict.path, 'diff')}
              >
                Diff View
              </button>
              <button
                type="button"
                class="view-tab"
                class:active={currentViewMode === 'sidebyside'}
                on:click={() => setViewMode(conflict.path, 'sidebyside')}
              >
                Side by Side
              </button>
              <button
                type="button"
                class="view-tab"
                class:active={currentViewMode === 'edit'}
                on:click={() => setViewMode(conflict.path, 'edit')}
              >
                Edit
              </button>
            </div>

            {#if currentViewMode === 'diff'}
              <div class="diff-view">
                <div class="diff-panel ours">
                  <div class="diff-panel-header ours">
                    <span class="branch-label">{oursLabel}</span>
                    <button
                      type="button"
                      class="use-btn"
                      class:selected={getResolution(conflict.path)?.choice === 'ours'}
                      on:click={() => chooseVersion(conflict.path, 'ours')}
                      disabled={conflict.ours_content === null}
                    >
                      {getResolution(conflict.path)?.choice === 'ours' ? '✓ Selected' : 'Use This'}
                    </button>
                  </div>
                  <div
                    class="diff-content"
                    use:scrollSync={{ path: conflict.path, side: 'ours' }}
                  >
                    {#each diff.oursChanges as line}
                      <div class="diff-line {line.type}">
                        <span class="line-num">{line.type === 'del' ? '' : line.lineNum || ''}</span>
                        <span class="line-prefix">{line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' '}</span>
                        <span class="line-content">{line.content}</span>
                      </div>
                    {/each}
                    {#if diff.oursChanges.length === 0}
                      <div class="empty-diff">(file deleted or empty)</div>
                    {/if}
                  </div>
                </div>

                <div class="diff-panel theirs">
                  <div class="diff-panel-header theirs">
                    <span class="branch-label">{theirsLabel}</span>
                    <button
                      type="button"
                      class="use-btn"
                      class:selected={getResolution(conflict.path)?.choice === 'theirs'}
                      on:click={() => chooseVersion(conflict.path, 'theirs')}
                      disabled={conflict.theirs_content === null}
                    >
                      {getResolution(conflict.path)?.choice === 'theirs' ? '✓ Selected' : 'Use This'}
                    </button>
                  </div>
                  <div
                    class="diff-content"
                    use:scrollSync={{ path: conflict.path, side: 'theirs' }}
                  >
                    {#each diff.theirsChanges as line}
                      <div class="diff-line {line.type}">
                        <span class="line-num">{line.type === 'del' ? '' : line.lineNum || ''}</span>
                        <span class="line-prefix">{line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' '}</span>
                        <span class="line-content">{line.content}</span>
                      </div>
                    {/each}
                    {#if diff.theirsChanges.length === 0}
                      <div class="empty-diff">(file deleted or empty)</div>
                    {/if}
                  </div>
                </div>
              </div>

              {#if conflict.base_content}
                <details class="base-content">
                  <summary>Show Base (common ancestor)</summary>
                  <pre class="code-block">{conflict.base_content}</pre>
                </details>
              {/if}

            {:else if currentViewMode === 'sidebyside'}
              <div class="sidebyside-view">
                <div class="side-panel">
                  <div class="side-header ours">
                    <span>{oursLabel}</span>
                    <button
                      type="button"
                      class="use-btn"
                      class:selected={getResolution(conflict.path)?.choice === 'ours'}
                      on:click={() => chooseVersion(conflict.path, 'ours')}
                      disabled={conflict.ours_content === null}
                    >
                      {getResolution(conflict.path)?.choice === 'ours' ? '✓ Selected' : 'Use This'}
                    </button>
                  </div>
                  <pre
                    class="code-block"
                    use:scrollSync={{ path: conflict.path + '-sbs', side: 'ours' }}
                  >{conflict.ours_content || '(file deleted)'}</pre>
                </div>
                <div class="side-panel">
                  <div class="side-header theirs">
                    <span>{theirsLabel}</span>
                    <button
                      type="button"
                      class="use-btn"
                      class:selected={getResolution(conflict.path)?.choice === 'theirs'}
                      on:click={() => chooseVersion(conflict.path, 'theirs')}
                      disabled={conflict.theirs_content === null}
                    >
                      {getResolution(conflict.path)?.choice === 'theirs' ? '✓ Selected' : 'Use This'}
                    </button>
                  </div>
                  <pre
                    class="code-block"
                    use:scrollSync={{ path: conflict.path + '-sbs', side: 'theirs' }}
                  >{conflict.theirs_content || '(file deleted)'}</pre>
                </div>
              </div>

            {:else if currentViewMode === 'edit'}
              <div class="edit-view">
                <div class="edit-header">
                  <span>Edit the final content:</span>
                  <div class="edit-actions">
                    <button
                      type="button"
                      class="copy-btn"
                      on:click={() => {
                        if (conflict.ours_content) setCustomContent(conflict.path, conflict.ours_content);
                      }}
                      disabled={!conflict.ours_content}
                    >
                      Start from {targetBranch}
                    </button>
                    <button
                      type="button"
                      class="copy-btn"
                      on:click={() => {
                        if (conflict.theirs_content) setCustomContent(conflict.path, conflict.theirs_content);
                      }}
                      disabled={!conflict.theirs_content}
                    >
                      Start from {sourceBranch}
                    </button>
                  </div>
                </div>
                <textarea
                  class="custom-editor"
                  value={getEditorContent(conflict)}
                  on:input={(e) => setCustomContent(conflict.path, e.currentTarget.value)}
                  placeholder="Edit the merged content here..."
                ></textarea>
              </div>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  </div>

  <div class="conflict-footer">
    <div class="footer-hint">
      {#if conflicts.every(c => resolutions.has(c.path))}
        <span class="ready">All conflicts resolved - ready to complete</span>
      {:else}
        <span class="pending">{conflicts.filter(c => !resolutions.has(c.path)).length} file(s) still need resolution</span>
      {/if}
    </div>
    <div class="footer-actions">
      <button type="button" class="btn-cancel" on:click={onCancel}>Cancel</button>
      <button
        type="button"
        class="btn-resolve"
        on:click={handleResolve}
        disabled={!conflicts.every(c => resolutions.has(c.path))}
      >
        {operation === 'rebase' ? 'Complete Rebase' : 'Complete Merge'}
      </button>
    </div>
  </div>
</div>

<style>
  .conflict-resolver {
    background: var(--surface-alt, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 8px;
    margin: 1rem 0;
    overflow: hidden;
  }

  .conflict-header {
    padding: 1rem;
    border-bottom: 1px solid var(--border-color, #45475a);
    background: var(--surface-color, #1e1e2e);
  }

  .header-main {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .conflict-header h3 {
    margin: 0;
    color: #f9e2af;
    font-size: 1.1rem;
  }

  .toggle-explanation {
    background: none;
    border: 1px solid var(--border-color, #45475a);
    color: var(--text-muted, #6c7086);
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
  }

  .toggle-explanation:hover {
    color: var(--text-color, #cdd6f4);
    border-color: var(--text-muted, #6c7086);
  }

  .operation-summary {
    margin: 0.5rem 0 0.25rem 0;
    color: var(--text-color, #cdd6f4);
    font-size: 0.9rem;
  }

  .conflict-count {
    margin: 0;
    color: var(--text-muted, #6c7086);
    font-size: 0.85rem;
  }

  /* Explanation Panel */
  .explanation-panel {
    background: var(--surface-color, #1e1e2e);
    border-bottom: 1px solid var(--border-color, #45475a);
    padding: 1rem;
  }

  .explanation-content {
    font-size: 0.85rem;
    color: var(--text-color, #cdd6f4);
  }

  .explanation-title {
    font-weight: 600;
    margin-bottom: 0.5rem;
    color: var(--text-color, #cdd6f4);
  }

  .explanation-content p {
    margin: 0.5rem 0;
  }

  .explanation-content code {
    background: var(--surface-alt, #313244);
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-family: monospace;
    color: #89b4fa;
  }

  .git-graph {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin: 1rem 0;
    padding: 1rem;
    background: var(--surface-alt, #181825);
    border-radius: 6px;
    overflow-x: auto;
  }

  .graph-before, .graph-after {
    flex: 1;
    min-width: 200px;
  }

  .graph-label {
    font-size: 0.75rem;
    color: var(--text-muted, #6c7086);
    margin-bottom: 0.5rem;
  }

  .graph-visual {
    font-family: monospace;
    font-size: 0.75rem;
    line-height: 1.4;
    color: var(--text-color, #cdd6f4);
    margin: 0;
    white-space: pre;
  }

  .graph-arrow {
    font-size: 1.5rem;
    color: #a6e3a1;
    flex-shrink: 0;
  }

  .conflict-explanation {
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color, #45475a);
  }

  .conflict-explanation ul {
    margin: 0.5rem 0;
    padding-left: 1.5rem;
  }

  .conflict-explanation li {
    margin: 0.25rem 0;
  }

  .label-ours {
    background: rgba(137, 180, 250, 0.2);
    color: #89b4fa;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-family: monospace;
    font-size: 0.8rem;
  }

  .label-theirs {
    background: rgba(203, 166, 247, 0.2);
    color: #cba6f7;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-family: monospace;
    font-size: 0.8rem;
  }

  .recommendation {
    margin-top: 0.75rem;
    padding: 0.75rem;
    background: rgba(166, 227, 161, 0.1);
    border: 1px solid rgba(166, 227, 161, 0.3);
    border-radius: 4px;
    font-size: 0.85rem;
  }

  .recommendation strong {
    color: #a6e3a1;
  }

  /* File list */
  .conflict-files {
    max-height: 500px;
    overflow-y: auto;
  }

  .conflict-file {
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .conflict-file:last-child {
    border-bottom: none;
  }

  .file-header {
    width: 100%;
    padding: 0.75rem 1rem;
    background: var(--surface-color, #1e1e2e);
    border: none;
    color: var(--text-color, #cdd6f4);
    text-align: left;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    transition: background 0.2s;
  }

  .file-header:hover {
    background: var(--hover-color, #313244);
  }

  .expand-icon {
    color: var(--text-muted, #6c7086);
    font-size: 0.8rem;
    width: 1rem;
  }

  .file-path {
    flex: 1;
    font-family: monospace;
    font-size: 0.9rem;
  }

  .resolved-badge {
    background: rgba(166, 227, 161, 0.2);
    color: #a6e3a1;
    padding: 0.2rem 0.6rem;
    border-radius: 3px;
    font-size: 0.75rem;
  }

  .unresolved-badge {
    background: rgba(249, 226, 175, 0.2);
    color: #f9e2af;
    padding: 0.2rem 0.6rem;
    border-radius: 3px;
    font-size: 0.75rem;
  }

  .conflict-content {
    background: var(--surface-alt, #181825);
  }

  .view-tabs {
    display: flex;
    gap: 0;
    padding: 0.5rem 1rem;
    border-bottom: 1px solid var(--border-color, #45475a);
  }

  .view-tab {
    padding: 0.5rem 1rem;
    background: transparent;
    border: 1px solid var(--border-color, #45475a);
    border-bottom: none;
    color: var(--text-muted, #6c7086);
    cursor: pointer;
    font-size: 0.85rem;
    margin-bottom: -1px;
  }

  .view-tab:first-child {
    border-radius: 4px 0 0 0;
  }

  .view-tab:last-child {
    border-radius: 0 4px 0 0;
  }

  .view-tab.active {
    background: var(--surface-alt, #181825);
    color: var(--text-color, #cdd6f4);
    border-bottom: 1px solid var(--surface-alt, #181825);
  }

  .view-tab:hover:not(.active) {
    color: var(--text-color, #cdd6f4);
  }

  /* Diff View */
  .diff-view {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0;
  }

  .diff-panel {
    border-right: 1px solid var(--border-color, #45475a);
  }

  .diff-panel:last-child {
    border-right: none;
  }

  .diff-panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem 0.75rem;
    font-size: 0.8rem;
    font-weight: 500;
  }

  .diff-panel-header.ours {
    background: rgba(137, 180, 250, 0.15);
    color: #89b4fa;
  }

  .diff-panel-header.theirs {
    background: rgba(203, 166, 247, 0.15);
    color: #cba6f7;
  }

  .branch-label {
    font-size: 0.8rem;
  }

  .use-btn {
    padding: 0.25rem 0.5rem;
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    color: var(--text-color, #cdd6f4);
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
  }

  .use-btn:hover:not(:disabled) {
    background: var(--hover-color, #313244);
  }

  .use-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .use-btn.selected {
    background: #a6e3a1;
    color: #1e1e2e;
    border-color: #a6e3a1;
  }

  .diff-content {
    font-family: monospace;
    font-size: 0.8rem;
    max-height: 350px;
    overflow: auto;
  }

  .diff-line {
    display: flex;
    padding: 0 0.5rem;
    line-height: 1.5;
  }

  /* Ours panel (target branch) - blue theme */
  .diff-panel.ours .diff-line.add {
    background: rgba(137, 180, 250, 0.15);
    border-left: 3px solid #89b4fa;
  }

  .diff-panel.ours .diff-line.del {
    background: rgba(243, 139, 168, 0.08);
    border-left: 3px solid rgba(243, 139, 168, 0.4);
  }

  .diff-panel.ours .diff-line.add .line-prefix {
    color: #89b4fa;
    font-weight: 600;
  }

  .diff-panel.ours .diff-line.add .line-content {
    color: #89b4fa;
  }

  .diff-panel.ours .diff-line.del .line-prefix {
    color: #f38ba8;
  }

  .diff-panel.ours .diff-line.del .line-content {
    color: var(--text-muted, #6c7086);
    text-decoration: line-through;
    opacity: 0.6;
  }

  /* Theirs panel (source/your branch) - purple theme */
  .diff-panel.theirs .diff-line.add {
    background: rgba(203, 166, 247, 0.15);
    border-left: 3px solid #cba6f7;
  }

  .diff-panel.theirs .diff-line.del {
    background: rgba(243, 139, 168, 0.08);
    border-left: 3px solid rgba(243, 139, 168, 0.4);
  }

  .diff-panel.theirs .diff-line.add .line-prefix {
    color: #cba6f7;
    font-weight: 600;
  }

  .diff-panel.theirs .diff-line.add .line-content {
    color: #cba6f7;
  }

  .diff-panel.theirs .diff-line.del .line-prefix {
    color: #f38ba8;
  }

  .diff-panel.theirs .diff-line.del .line-content {
    color: var(--text-muted, #6c7086);
    text-decoration: line-through;
    opacity: 0.6;
  }

  .line-num {
    width: 3rem;
    text-align: right;
    padding-right: 0.5rem;
    color: var(--text-muted, #6c7086);
    user-select: none;
    flex-shrink: 0;
  }

  .line-prefix {
    width: 1rem;
    flex-shrink: 0;
    color: var(--text-muted, #6c7086);
  }

  .line-content {
    white-space: pre;
    flex: 1;
  }

  .empty-diff {
    padding: 1rem;
    color: var(--text-muted, #6c7086);
    font-style: italic;
    text-align: center;
  }

  .base-content {
    margin: 0.5rem;
    padding: 0.5rem;
    background: var(--surface-color, #1e1e2e);
    border-radius: 4px;
  }

  .base-content summary {
    cursor: pointer;
    color: var(--text-muted, #6c7086);
    font-size: 0.85rem;
    padding: 0.25rem;
  }

  .base-content summary:hover {
    color: var(--text-color, #cdd6f4);
  }

  /* Side by Side View */
  .sidebyside-view {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0;
  }

  .side-panel {
    border-right: 1px solid var(--border-color, #45475a);
  }

  .side-panel:last-child {
    border-right: none;
  }

  .side-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem 0.75rem;
    font-size: 0.85rem;
    font-weight: 500;
  }

  .side-header.ours {
    background: rgba(137, 180, 250, 0.15);
    color: #89b4fa;
  }

  .side-header.theirs {
    background: rgba(203, 166, 247, 0.15);
    color: #cba6f7;
  }

  .code-block {
    background: var(--surface-color, #1e1e2e);
    padding: 0.75rem;
    margin: 0;
    font-family: monospace;
    font-size: 0.8rem;
    color: var(--text-color, #cdd6f4);
    overflow: auto;
    max-height: 350px;
    white-space: pre;
  }

  /* Edit View */
  .edit-view {
    padding: 1rem;
  }

  .edit-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .edit-header span {
    color: var(--text-muted, #6c7086);
    font-size: 0.85rem;
  }

  .edit-actions {
    display: flex;
    gap: 0.5rem;
  }

  .copy-btn {
    padding: 0.25rem 0.5rem;
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    color: var(--text-color, #cdd6f4);
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
  }

  .copy-btn:hover:not(:disabled) {
    background: var(--hover-color, #313244);
  }

  .copy-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .custom-editor {
    width: 100%;
    min-height: 300px;
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    padding: 0.75rem;
    color: var(--text-color, #cdd6f4);
    font-family: monospace;
    font-size: 0.8rem;
    resize: vertical;
  }

  .custom-editor:focus {
    outline: none;
    border-color: var(--primary-color, #89b4fa);
  }

  /* Footer */
  .conflict-footer {
    padding: 1rem;
    border-top: 1px solid var(--border-color, #45475a);
    background: var(--surface-color, #1e1e2e);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .footer-hint {
    font-size: 0.85rem;
  }

  .footer-hint .ready {
    color: #a6e3a1;
  }

  .footer-hint .pending {
    color: #f9e2af;
  }

  .footer-actions {
    display: flex;
    gap: 0.5rem;
  }

  .btn-cancel {
    padding: 0.6rem 1.2rem;
    background: var(--surface-alt, #313244);
    border: 1px solid var(--border-color, #45475a);
    color: var(--text-color, #cdd6f4);
    border-radius: 4px;
    cursor: pointer;
  }

  .btn-cancel:hover {
    background: var(--hover-color, #45475a);
  }

  .btn-resolve {
    padding: 0.6rem 1.2rem;
    background: #a6e3a1;
    border: none;
    color: #1e1e2e;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 500;
  }

  .btn-resolve:hover:not(:disabled) {
    opacity: 0.9;
  }

  .btn-resolve:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
