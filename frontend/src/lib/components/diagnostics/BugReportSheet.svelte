<script lang="ts">
  /**
   * The bug-report review sheet.
   *
   * THE REVIEW IS THE SAFETY MECHANISM. NOT A NICETY.
   * -------------------------------------------------
   * The target repository is PUBLIC and GitHub issues are indexed. Automatic
   * redaction catches KNOWN credential shapes - `sk-ant-`, `ghp_`, `AKIA`,
   * bearer tokens, JWTs, PEM blocks. It cannot catch:
   *
   *   - an agent step log containing the source of a private repository
   *   - a prompt carrying a confidential product decision
   *   - a connection string of an unusual shape, or an internal hostname
   *   - a customer or employer name in a card title or a branch name
   *   - a host path carrying a username
   *
   * A denylist is necessary and insufficient. What compensates is that a
   * human reads the bundle before any of it is published, so this sheet is
   * built to make that reading actually happen rather than nominally exist:
   *
   *  1. THE ARTIFACT IS BUILT SERVER-SIDE FIRST, AND THIS RENDERS THAT
   *     ARTIFACT. `Preview` opens the assembled member; `Download` streams the
   *     same bundle id. There is no "we will redact it on the way out" step
   *     for a bug to hide in, and no second code path between what was
   *     reviewed and what was received.
   *
   *  2. ANY CHANGE REBUILDS. Toggling a source, or editing the summary,
   *     invalidates the bundle and builds a new one, and Download is disabled
   *     until it lands. A selection change that silently altered the download
   *     would hand the user bytes they never looked at - which is the whole
   *     failure this sheet exists to prevent.
   *
   *  3. THE RISKY SOURCES SAY SO, IN WORDS, NEXT TO THEMSELVES. Not a
   *     checkbox buried in a form and not a generic banner: a red sentence on
   *     the step-log row naming prompts, model output and source diffs.
   *
   *  4. THERE IS NO UPLOAD BUTTON. Not disabled - absent. See the note at the
   *     foot of this sheet.
   */
  import { createEventDispatcher } from 'svelte';
  import {
    diagnosticsApi,
    formatBytes,
    splitRedactions,
    type BundleManifest,
    type BundleMember,
  } from '../../stores/diagnostics';
  import { ApiError } from '../../api/client';

  export let runId: string | null = null;
  export let initialTitle = '';

  const dispatch = createEventDispatcher<{ close: void }>();

  /**
   * The source catalog.
   *
   * An ALLOWLIST, deliberately. A log surface added in a future phase is
   * absent from bundles until someone adds it here on purpose, with a test.
   * The bundle never widens on its own.
   */
  interface SourceDef {
    id: string;
    label: string;
    hint: string;
    defaultOn: boolean;
    /** Renders the red warning. Pattern redaction does not reach these. */
    risky?: boolean;
  }

  const SOURCES: SourceDef[] = [
    {
      id: 'system',
      label: 'System facts',
      hint: 'running code vs. code on disk, migrations, checkout',
      defaultOn: true,
    },
    {
      id: 'containers',
      label: 'Container inventory',
      hint: 'names, images, uptimes — never environment variables',
      defaultOn: true,
    },
    {
      id: 'backend',
      label: 'Backend log',
      hint: 'the application ring buffer',
      defaultOn: true,
    },
    {
      id: 'steps',
      label: 'Step logs for this run',
      hint: 'what the failing step printed',
      defaultOn: true,
      risky: true,
    },
    {
      id: 'runner',
      label: 'Runner agent log',
      hint: 'image pulls, docker faults, dispatch',
      defaultOn: true,
      risky: true,
    },
    {
      id: 'settings',
      label: 'Settings names',
      hint: 'names and present/absent only — never values',
      defaultOn: true,
    },
    {
      id: 'browser',
      label: 'Browser console',
      hint: 'failed requests: method, path, status — never bodies',
      defaultOn: false,
    },
  ];

  let title = initialTitle;
  let whatHappened = '';
  let enabled = new Set(SOURCES.filter((s) => s.defaultOn).map((s) => s.id));

  let manifest: BundleManifest | null = null;
  let building = false;
  let buildError: string | null = null;

  let previewName: string | null = null;
  let previewText: string | null = null;
  let previewError: string | null = null;
  let previewLoading = false;

  let downloading = false;
  let downloadError: string | null = null;
  let downloadedName: string | null = null;

  let rebuildTimer: ReturnType<typeof setTimeout> | null = null;
  /** Guards against an older build resolving after a newer one. */
  let buildToken = 0;

  async function build() {
    const token = ++buildToken;
    building = true;
    buildError = null;
    // The previous manifest is dropped the instant the inputs change, so the
    // sheet can never show member sizes belonging to a different selection.
    manifest = null;
    previewName = null;
    previewText = null;
    downloadedName = null;
    try {
      const built = await diagnosticsApi.buildBundle({
        title,
        what_happened: whatHappened,
        sources: [...enabled],
        run_id: runId,
      });
      if (token !== buildToken) return;
      manifest = built;
    } catch (e) {
      if (token !== buildToken) return;
      buildError =
        e instanceof ApiError && e.status === 404
          ? 'This backend does not expose /api/diagnostics/bundle, so no bundle can be built here.'
          : e instanceof Error
            ? e.message
            : String(e);
    } finally {
      if (token === buildToken) building = false;
    }
  }

  function scheduleRebuild() {
    if (rebuildTimer) clearTimeout(rebuildTimer);
    // Dropping the manifest immediately (not after the debounce) is what
    // keeps Download disabled during the gap between a change and its
    // rebuild. Leaving the stale manifest visible for 400ms would leave a
    // 400ms window in which Download shipped the pre-change bundle.
    manifest = null;
    rebuildTimer = setTimeout(() => void build(), 400);
  }

  function toggleSource(id: string) {
    const next = new Set(enabled);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    enabled = next;
    scheduleRebuild();
  }

  async function openPreview(member: BundleMember) {
    if (!manifest) return;
    previewName = member.name;
    previewText = null;
    previewError = null;
    previewLoading = true;
    try {
      previewText = await diagnosticsApi.member(manifest.id, member.name);
    } catch (e) {
      previewError = e instanceof Error ? e.message : String(e);
    } finally {
      previewLoading = false;
    }
  }

  /**
   * Fetch the archive as bytes rather than pointing a link at the URL.
   *
   * Bundles live in memory for 15 minutes; a dead `<a href>` would give the
   * user a browser error page on an expired id, where this gives them a
   * sentence telling them to build a new one.
   */
  async function download() {
    if (!manifest) return;
    downloading = true;
    downloadError = null;
    try {
      const blob = await diagnosticsApi.archive(manifest.id);
      const name = `lazyaf-bugreport-${manifest.id.slice(0, 8)}.zip`;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      // Revoking synchronously can cancel the download in some browsers.
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      downloadedName = name;
    } catch (e) {
      downloadError = e instanceof Error ? e.message : String(e);
    } finally {
      downloading = false;
    }
  }

  /**
   * Members indexed by source id, as a REACTIVE value.
   *
   * This was a plain `memberFor(id)` function called from the template's
   * {@const}, and it silently never updated: Svelte derives a {@const}'s
   * dependencies from the expression it can SEE, and `manifest` was read
   * inside the function body rather than in the expression. The const was
   * therefore computed once, while the build was still in flight and the
   * manifest was null, and every row rendered its placeholder hint forever -
   * no sizes, no redaction counts, no Preview button. Naming the dependency
   * here is what makes the rows track the bundle.
   */
  $: memberBySource = new Map<string, BundleMember>(
    (manifest?.members ?? []).map((m) => [m.source, m]),
  );

  // Build once when the sheet opens.
  void build();

  $: canDownload = !!manifest && !building && !downloading;
</script>

<svelte:window
  on:keydown={(e) => {
    if (e.key === 'Escape') dispatch('close');
  }}
/>

<div
  class="scrim"
  data-testid="bug-report-scrim"
  role="presentation"
  on:click|self={() => dispatch('close')}
>
  <div
    class="sheet"
    data-testid="bug-report-sheet"
    role="dialog"
    aria-modal="true"
    aria-label="Bug report"
  >
    <header class="sheet-head">
      <h2>Bug report</h2>
      <button class="close" data-testid="bug-report-close" on:click={() => dispatch('close')}
        >✕</button
      >
    </header>

    <div class="sheet-body">
      <div class="field">
        <label for="br-title">Title</label>
        <input
          id="br-title"
          data-testid="bug-report-title"
          bind:value={title}
          on:input={scheduleRebuild}
          placeholder="branch 'main' not found when starting card work"
        />
      </div>

      <div class="field">
        <label for="br-what">What happened</label>
        <textarea
          id="br-what"
          data-testid="bug-report-what"
          rows="3"
          bind:value={whatHappened}
          on:input={scheduleRebuild}
          placeholder="clicked Start on a card, got a 500"
        ></textarea>
      </div>

      <div class="included">
        <div class="included-head">
          <h3>Included</h3>
          {#if building}
            <span class="building" data-testid="bundle-building">
              <span class="spinner"></span> building…
            </span>
          {:else if manifest}
            <span class="totals" data-testid="bundle-totals">
              {formatBytes(manifest.total_bytes)} ·
              <b>{manifest.redacted_total}</b>
              {manifest.redacted_total === 1 ? 'value' : 'values'} redacted
            </span>
          {/if}
        </div>

        {#if buildError}
          <p class="build-error" data-testid="bundle-error">{buildError}</p>
        {/if}

        <ul class="members">
          {#each SOURCES as source}
            {@const member = memberBySource.get(source.id) ?? null}
            {@const on = enabled.has(source.id)}
            <li class="member" class:off={!on} data-testid="bundle-source-{source.id}">
              <label class="member-main">
                <input
                  type="checkbox"
                  checked={on}
                  data-testid="bundle-toggle-{source.id}"
                  on:change={() => toggleSource(source.id)}
                />
                <span class="member-label">{source.label}</span>
                {#if member}
                  <code class="member-name">{member.name}</code>
                  <span class="member-size">{formatBytes(member.bytes)}</span>
                {/if}
              </label>
              <div class="member-meta">
                <span class="hint">{member?.summary ?? source.hint}</span>
                {#if member && member.redacted > 0}
                  <span class="redaction-count" data-testid="member-redactions-{source.id}">
                    {member.redacted} redacted
                  </span>
                {/if}
                {#if member}
                  <button class="preview-link" on:click={() => openPreview(member)}>
                    Preview
                  </button>
                {/if}
              </div>
              {#if source.risky && on}
                <!--
                  The risky source is labelled where it lives, in words, every
                  time. This is the sentence pattern redaction cannot make
                  true, so it has to be read.
                -->
                <p class="risk" data-testid="risk-note-{source.id}">
                  Contains your prompts, model output and diffs of your source. Automatic
                  redaction cannot remove any of that. Read it.
                </p>
              {/if}
            </li>
          {/each}
        </ul>
      </div>

      {#if previewName}
        <div class="preview" data-testid="bundle-preview">
          <div class="preview-head">
            <h3>{previewName}</h3>
            <span class="preview-note">
              This is the assembled file, not a rehearsal of it — the bundle you download
              contains exactly these bytes.
            </span>
            <button class="close" on:click={() => (previewName = null)}>✕</button>
          </div>
          {#if previewLoading}
            <div class="preview-body muted"><span class="spinner"></span> loading…</div>
          {:else if previewError}
            <div class="preview-body error">{previewError}</div>
          {:else if previewText !== null}
            <pre class="preview-body" data-testid="bundle-preview-text">{#each splitRedactions(previewText) as segment}{#if segment.redacted}<span
                    class="redacted"
                    data-testid="preview-redacted-span">{segment.text}</span
                  >{:else}{segment.text}{/if}{/each}</pre>
          {/if}
        </div>
      {/if}

      {#if downloadError}
        <p class="build-error" data-testid="download-error">{downloadError}</p>
      {/if}
      {#if downloadedName}
        <p class="downloaded" data-testid="download-done">
          Saved <code>{downloadedName}</code>. Attach it to the issue by dragging it into
          GitHub's comment box.
        </p>
      {/if}
    </div>

    <footer class="sheet-foot">
      <!--
        THE WARNING LIVES IN THE FOOTER, NOT IN THE SCROLLING BODY.
        It was below the member list, which put the single most important
        sentence for a PUBLIC repository below the fold: the list is long
        enough that a reader could tick every source and press Download
        without the caveat ever having been on screen. The footer is pinned,
        so it sits beside the button it qualifies and cannot be scrolled past.
      -->
      <div class="warning" data-testid="public-repo-warning">
        <strong>Redaction removes known credential shapes. It cannot remove everything.</strong>
        <p>
          Your source code, your prompts, internal hostnames and customer or employer
          names are not credential-shaped and will not be redacted. The LazyAF
          repository is <b>public</b> and GitHub issues are indexed. Read the bundle
          before you attach it to anything.
        </p>
      </div>
      <!--
        NO UPLOAD BUTTON, AND ITS ABSENCE IS THE DESIGN.
        Creating an issue needs a write-scoped GitHub token held by a process
        whose every human-facing router is unauthenticated and which binds
        0.0.0.0 with the docker socket mounted. A disabled button is an
        invitation to hunt for the flag that enables it; an explained absence
        is a decision. The downloaded bundle needs no credential at all and is
        attachable by hand today.
      -->
      <p class="no-upload" data-testid="no-upload-note">
        There is no upload button. Posting to GitHub from here would need a write-scoped
        token in a backend that has no authentication — and a human pressing Submit on
        GitHub's own page is what keeps the review above from being optional.
      </p>
      <div class="actions">
        <button class="btn-ghost" on:click={() => dispatch('close')}>Cancel</button>
        <button
          class="btn-primary"
          data-testid="bug-report-download"
          disabled={!canDownload}
          on:click={download}
        >
          {#if downloading}Preparing…{:else if building}Building…{:else}Download bundle (.zip){/if}
        </button>
      </div>
    </footer>
  </div>
</div>

<style>
  .scrim {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
    padding: 2rem 1rem;
  }

  .sheet {
    background: var(--surface-color, #1e1e2e);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 10px;
    width: min(760px, 100%);
    max-height: 100%;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  }

  .sheet-head,
  .sheet-foot {
    padding: 0.85rem 1.1rem;
    border-bottom: 1px solid var(--border-color, #45475a);
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }
  .sheet-foot {
    border-bottom: none;
    border-top: 1px solid var(--border-color, #45475a);
    flex-direction: column;
    align-items: stretch;
    gap: 0.6rem;
  }

  .sheet-head h2 {
    margin: 0;
    font-size: 1rem;
    color: var(--text-color, #cdd6f4);
  }

  .close {
    margin-left: auto;
    background: none;
    border: none;
    color: var(--text-muted, #a6adc8);
    font-size: 0.9rem;
    cursor: pointer;
  }
  .close:hover {
    color: var(--text-color, #cdd6f4);
  }

  .sheet-body {
    padding: 1rem 1.1rem;
    overflow-y: auto;
    flex: 1;
    min-height: 4rem;
  }

  .field {
    margin-bottom: 0.8rem;
  }
  .field label {
    display: block;
    font-size: 0.75rem;
    color: var(--text-muted, #a6adc8);
    margin-bottom: 0.25rem;
  }
  .field input,
  .field textarea {
    width: 100%;
    padding: 0.45rem 0.6rem;
    background: var(--input-bg, #313244);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 5px;
    color: var(--text-color, #cdd6f4);
    font-size: 0.85rem;
    font-family: inherit;
    resize: vertical;
  }

  .included-head {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin-bottom: 0.4rem;
  }
  .included-head h3 {
    margin: 0;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--text-muted, #a6adc8);
  }
  .totals,
  .building {
    font-size: 0.75rem;
    color: var(--text-muted, #a6adc8);
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }

  .members {
    list-style: none;
    margin: 0;
    padding: 0;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
  }

  .member {
    padding: 0.5rem 0.65rem;
    border-bottom: 1px solid var(--border-color, #45475a);
  }
  .member:last-child {
    border-bottom: none;
  }
  .member.off {
    opacity: 0.55;
  }

  .member-main {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-size: 0.85rem;
    color: var(--text-color, #cdd6f4);
  }
  .member-label {
    font-weight: 500;
  }
  .member-name {
    font-family: var(--font-mono, monospace);
    font-size: 0.72rem;
    color: var(--text-muted, #a6adc8);
    background: var(--surface-alt, #181825);
    padding: 0.05rem 0.3rem;
    border-radius: 3px;
  }
  .member-size {
    margin-left: auto;
    font-size: 0.74rem;
    color: var(--text-muted, #6c7086);
  }

  .member-meta {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-top: 0.2rem;
    padding-left: 1.4rem;
    font-size: 0.74rem;
    color: var(--text-muted, #6c7086);
  }

  .redaction-count {
    color: #cba6f7;
    background: rgba(203, 166, 247, 0.15);
    border-radius: 3px;
    padding: 0 0.3rem;
  }

  .preview-link {
    background: none;
    border: none;
    color: var(--primary-color, #89b4fa);
    font-size: 0.74rem;
    cursor: pointer;
    padding: 0;
    text-decoration: underline;
  }

  .risk {
    margin: 0.35rem 0 0;
    padding-left: 1.4rem;
    font-size: 0.74rem;
    color: var(--error-color, #f38ba8);
    font-weight: 500;
  }

  .preview {
    margin-top: 0.85rem;
    border: 1px solid var(--border-color, #45475a);
    border-radius: 6px;
    overflow: hidden;
  }
  .preview-head {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.45rem 0.65rem;
    background: var(--surface-alt, #181825);
    border-bottom: 1px solid var(--border-color, #45475a);
  }
  .preview-head h3 {
    margin: 0;
    font-size: 0.78rem;
    font-family: var(--font-mono, monospace);
    color: var(--text-color, #cdd6f4);
  }
  .preview-note {
    font-size: 0.7rem;
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }
  .preview-body {
    margin: 0;
    padding: 0.6rem 0.7rem;
    max-height: 260px;
    overflow: auto;
    font-family: var(--font-mono, monospace);
    font-size: 0.72rem;
    line-height: 1.5;
    color: var(--text-color, #cdd6f4);
    background: var(--bg-color, #11111b);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .preview-body.muted {
    color: var(--text-muted, #a6adc8);
  }
  .preview-body.error {
    color: var(--error-color, #f38ba8);
  }

  .redacted {
    background: rgba(203, 166, 247, 0.18);
    border: 1px solid rgba(203, 166, 247, 0.45);
    border-radius: 3px;
    padding: 0 0.2rem;
    color: #cba6f7;
    font-weight: 600;
  }

  .warning {
    padding: 0.55rem 0.7rem;
    border: 1px solid var(--warning-color, #f9e2af);
    border-radius: 6px;
    background: rgba(249, 226, 175, 0.08);
  }
  .warning strong {
    display: block;
    font-size: 0.8rem;
    color: var(--warning-color, #f9e2af);
  }
  .warning p {
    margin: 0.25rem 0 0;
    font-size: 0.76rem;
    color: var(--text-muted, #a6adc8);
  }

  .build-error {
    margin: 0.6rem 0 0;
    font-size: 0.78rem;
    color: var(--error-color, #f38ba8);
  }
  .downloaded {
    margin: 0.6rem 0 0;
    font-size: 0.78rem;
    color: var(--success-color, #a6e3a1);
  }
  .downloaded code {
    font-family: var(--font-mono, monospace);
    font-size: 0.72rem;
  }

  .no-upload {
    margin: 0;
    font-size: 0.73rem;
    color: var(--text-muted, #6c7086);
    line-height: 1.45;
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }

  .btn-ghost,
  .btn-primary {
    padding: 0.45rem 0.9rem;
    border-radius: 5px;
    font-size: 0.83rem;
    cursor: pointer;
    border: 1px solid var(--border-color, #45475a);
  }
  .btn-ghost {
    background: none;
    color: var(--text-muted, #a6adc8);
  }
  .btn-primary {
    background: var(--primary-color, #89b4fa);
    color: var(--primary-text, #11111b);
    border-color: transparent;
    font-weight: 600;
  }
  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .spinner {
    width: 11px;
    height: 11px;
    border: 2px solid var(--border-color, #45475a);
    border-top-color: var(--primary-color, #89b4fa);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    display: inline-block;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
