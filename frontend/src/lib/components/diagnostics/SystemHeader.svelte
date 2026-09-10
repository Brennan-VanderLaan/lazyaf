<script lang="ts">
  /**
   * "What is actually running here" - above the logs, before anything is
   * selected.
   *
   * This strip exists because of a two-day bug. A backend container was
   * serving 25-hour-old code while the files on disk were current: compose
   * does not recreate a container whose config is unchanged, and
   * `backend/app` is bind-mounted, so the code on disk and the code being
   * executed had silently diverged. The symptom was "branch not found" on a
   * repository whose branch plainly existed, and nothing in the product could
   * tell the difference between that and a real git fault.
   *
   * If this strip is green the user stops reading it. If it is red they have
   * their answer in one glance, with the command that fixes it. That is most
   * of the value of the entire feature, which is why it is not behind a tab,
   * a toggle or a fetch the user has to ask for.
   *
   * NOTHING HERE INVENTS A VALUE (R1/R4). "We could not tell" renders as its
   * own state, never as "fine". A green strip about staleness that we made up
   * would be green about the one fact this exists to report.
   */
  import {
    oldestContainer,
    type ContainerFacts,
    type SystemFacts,
    type LoadStatus,
    type SystemVerdict,
  } from '../../stores/diagnostics';
  import { formatAge, formatDateTime, formatDuration, UNKNOWN } from '../../utils/time';

  /**
   * Uptimes use `formatDuration`, NOT `formatAge`.
   *
   * `formatAge` is the coarse single-unit form ("4m", "3h", "1d") and it is
   * right for "checked 12s ago". It is wrong for every number in this panel:
   * the backend at 25h, the frontend at 28h and the runner at 39h all collapse
   * to "1d", which erases the exact discrimination the strip exists to make -
   * the reader is here to spot the container that is older than the others.
   * `formatDuration` keeps hour-and-minute precision at any age.
   */
  const uptime = (at: string | null | undefined, now: number) =>
    at ? formatDuration(at, null, now) : UNKNOWN;

  export let system: SystemFacts | null = null;
  export let status: LoadStatus = 'idle';
  export let verdict: SystemVerdict = 'unknown';
  export let error: string | null = null;
  export let unavailable = false;
  export let nowTick: number = Date.now();

  let showContainers = false;

  $: oldest = system ? oldestContainer(system.containers) : null;
  $: stepContainers = system
    ? system.containers.filter((c) => (c.service ?? c.name).includes('step'))
    : [];
  $: namedContainers = system
    ? system.containers.filter((c) => !(c.service ?? c.name).includes('step'))
    : [];

  function containerLabel(c: ContainerFacts): string {
    return c.service ?? c.name;
  }

  /** Image ids are long; the first 12 hex is what `docker images` shows. */
  function shortImageId(id: string): string {
    const bare = id.startsWith('sha256:') ? id.slice(7) : id;
    return bare.slice(0, 12);
  }
</script>

<section class="system" data-testid="system-header" data-verdict={verdict}>
  <header class="system-head">
    <h2>System</h2>
    {#if system}
      <span class="generated" title={formatDateTime(system.generated_at)}>
        checked {formatAge(system.generated_at, nowTick)} ago
      </span>
    {/if}
    <slot name="actions" />
  </header>

  {#if unavailable}
    <!--
      A 404 from the diagnostics router means this BACKEND PREDATES the
      feature. That is not "everything is fine" and it is not "the probe
      failed"; it is a missing capability, and naming the routes turns a dead
      page into an actionable one.
    -->
    <div class="row row-unknown" data-testid="system-unavailable">
      <span class="glyph">—</span>
      <div class="row-body">
        <strong>This backend does not expose the diagnostics API.</strong>
        <p>
          The Logs tab needs <code>/api/diagnostics/system</code> and
          <code>/api/diagnostics/logs</code>, which this backend does not serve. Nothing
          below is being reported, and no claim is being made about whether this
          stack is healthy.
        </p>
      </div>
    </div>
  {:else if status === 'error'}
    <div class="row row-error" data-testid="system-error">
      <span class="glyph">✕</span>
      <div class="row-body">
        <strong>Could not read system state.</strong>
        <p>{error}</p>
      </div>
    </div>
  {:else if status === 'loading' || status === 'idle'}
    <div class="row row-unknown" data-testid="system-loading">
      <span class="spinner"></span>
      <div class="row-body"><strong>Checking what is running…</strong></div>
    </div>
  {:else if system}
    {#if system.staleness.stale}
      <!-- THE headline. The bug that cost two days, named in one line. -->
      <div class="row row-bad" data-testid="system-staleness">
        <span class="glyph">⚠</span>
        <div class="row-body">
          <strong>Running code is not the code on disk.</strong>
          <p>
            The backend process started
            <b>{uptime(system.staleness.process_started_at, nowTick)} ago</b>;
            {system.staleness.stale_file_count}
            {system.staleness.stale_file_count === 1 ? 'file has' : 'files have'} changed
            since. This process is not executing them.
          </p>
          {#if system.staleness.stale_files.length}
            <ul class="files" data-testid="stale-files">
              {#each system.staleness.stale_files.slice(0, 6) as file}
                <li><code>{file}</code></li>
              {/each}
              {#if system.staleness.stale_files.length > 6}
                <li class="more">
                  and {system.staleness.stale_files.length - 6} more
                </li>
              {/if}
            </ul>
          {/if}
          {#if system.staleness.remedy}
            <pre class="remedy" data-testid="stale-remedy">{system.staleness.remedy}</pre>
          {/if}
        </div>
      </div>
    {:else}
      <div class="row row-good" data-testid="system-staleness">
        <span class="glyph">✓</span>
        <div class="row-body">
          <strong>Running code matches the code on disk.</strong>
          <p>
            Backend process started
            {uptime(system.staleness.process_started_at, nowTick)} ago; no application
            file is newer than it.
          </p>
        </div>
      </div>
    {/if}

    <div class="row {system.migrations.ok ? 'row-good' : 'row-bad'}" data-testid="system-migrations">
      <span class="glyph">{system.migrations.ok ? '✓' : '⚠'}</span>
      <div class="row-body">
        <strong>Migrations</strong>
        <p>
          database at <code>{system.migrations.db_head ?? UNKNOWN}</code>;
          {#if system.migrations.unapplied_count > 0}
            <b>{system.migrations.unapplied_count} unapplied on disk</b>
          {:else}
            up to date with the revisions on disk
          {/if}
          {#if system.migrations.gaps.length}
            · <b>gap in the chain: {system.migrations.gaps.join(', ')}</b>
          {/if}
        </p>
      </div>
    </div>

    <div
      class="row {system.checkout.available ? 'row-good' : 'row-unknown'}"
      data-testid="system-checkout"
    >
      <span class="glyph">{system.checkout.available ? '✓' : '—'}</span>
      <div class="row-body">
        <strong>Checkout</strong>
        {#if system.checkout.available}
          <p>
            <code>{system.checkout.sha?.slice(0, 12)}</code>
            {#if system.checkout.dirty}
              · <b
                >dirty, {system.checkout.dirty_file_count} modified {system.checkout
                  .dirty_file_count === 1
                  ? 'file'
                  : 'files'}</b
              >
            {:else}
              · clean
            {/if}
          </p>
        {:else}
          <!--
            R1/R4: we do NOT substitute a build-time baked git SHA here. With a
            bind mount that value is a lie by construction - the image was
            built at one commit while the files being executed are at another,
            which is the very divergence this panel exists to detect.
          -->
          <p>{system.checkout.reason ?? 'Not visible from this container.'}</p>
        {/if}
      </div>
    </div>

    {#if system.container_error}
      <div class="row row-unknown" data-testid="system-container-error">
        <span class="glyph">—</span>
        <div class="row-body">
          <strong>Containers could not be listed.</strong>
          <p>{system.container_error}</p>
        </div>
      </div>
    {:else if system.containers.length}
      <div class="containers" data-testid="system-containers">
        <button
          class="containers-toggle"
          data-testid="containers-toggle"
          aria-expanded={showContainers}
          on:click={() => (showContainers = !showContainers)}
        >
          <span class="chev">{showContainers ? '▾' : '▸'}</span>
          {system.containers.length} LazyAF {system.containers.length === 1
            ? 'container'
            : 'containers'}
          {#if oldest}
            · oldest <code>{containerLabel(oldest)}</code>
            {uptime(oldest.started_at, nowTick)}
          {/if}
        </button>

        {#if showContainers}
          <table class="container-table">
            <thead>
              <tr><th>service</th><th>started</th><th>image</th><th>restarts</th><th>health</th></tr>
            </thead>
            <tbody>
              {#each namedContainers as c}
                <tr class:is-oldest={oldest && c.name === oldest.name}>
                  <td><code>{containerLabel(c)}</code></td>
                  <td>
                    {uptime(c.started_at, nowTick)}
                    {#if oldest && c.name === oldest.name}<span class="oldest-tag">oldest</span>{/if}
                  </td>
                  <td><code class="img" title={c.image}>{shortImageId(c.image_id)}</code></td>
                  <td>{c.restart_count}</td>
                  <td>{c.health ?? '—'}</td>
                </tr>
              {/each}
              {#if stepContainers.length}
                <tr class="grouped">
                  <td><code>{stepContainers.length} step containers</code></td>
                  <td>{uptime(oldestContainer(stepContainers)?.started_at, nowTick)}</td>
                  <td>
                    {new Set(stepContainers.map((c) => c.image_id)).size} images
                  </td>
                  <td>—</td>
                  <td>—</td>
                </tr>
              {/if}
            </tbody>
          </table>
        {/if}

        {#if system.other_containers_on_host > 0}
          <!--
            Count only, never names. This machine runs containers belonging to
            other projects, and publishing their names into a public issue
            would leak the existence of unrelated work for no diagnostic gain.
          -->
          <p class="others" data-testid="other-containers">
            {system.other_containers_on_host} other containers on this host are not part of
            LazyAF. They are counted here and are not collected, named or included in a
            bug report.
          </p>
        {/if}
      </div>
    {/if}
  {/if}
</section>

<style>
  .system {
    border: 1px solid var(--border-color, #45475a);
    border-radius: 8px;
    background: var(--surface-color, #1e1e2e);
    padding: 0.75rem 1rem 0.85rem;
  }

  .system-head {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.6rem;
  }

  .system-head h2 {
    margin: 0;
    font-size: 0.8rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-muted, #a6adc8);
  }

  .generated {
    font-size: 0.72rem;
    color: var(--text-muted, #6c7086);
  }

  .system-head :global(.head-actions) {
    margin-left: auto;
  }

  .row {
    display: flex;
    gap: 0.65rem;
    padding: 0.45rem 0.6rem;
    border-radius: 6px;
    margin-bottom: 0.35rem;
    border-left: 3px solid transparent;
  }

  .row-good {
    border-left-color: var(--success-color, #a6e3a1);
  }
  .row-bad {
    border-left-color: var(--error-color, #f38ba8);
    background: rgba(243, 139, 168, 0.08);
  }
  .row-error {
    border-left-color: var(--error-color, #f38ba8);
    background: rgba(243, 139, 168, 0.08);
  }
  .row-unknown {
    border-left-color: var(--text-muted, #6c7086);
    background: rgba(108, 112, 134, 0.1);
  }

  .glyph {
    font-size: 0.95rem;
    line-height: 1.4;
  }
  .row-good .glyph {
    color: var(--success-color, #a6e3a1);
  }
  .row-bad .glyph,
  .row-error .glyph {
    color: var(--error-color, #f38ba8);
  }
  .row-unknown .glyph {
    color: var(--text-muted, #6c7086);
  }

  .row-body {
    flex: 1;
    min-width: 0;
  }

  .row-body strong {
    display: block;
    font-size: 0.85rem;
    color: var(--text-color, #cdd6f4);
  }

  .row-body p {
    margin: 0.15rem 0 0;
    font-size: 0.78rem;
    color: var(--text-muted, #a6adc8);
  }

  .row-bad .row-body strong {
    color: var(--error-color, #f38ba8);
  }

  code {
    font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
    font-size: 0.74rem;
    background: var(--surface-alt, #181825);
    padding: 0.05rem 0.3rem;
    border-radius: 3px;
  }

  .files {
    margin: 0.35rem 0 0;
    padding-left: 1rem;
    font-size: 0.74rem;
    color: var(--text-muted, #a6adc8);
  }
  .files .more {
    list-style: none;
    margin-left: -1rem;
    font-style: italic;
  }

  .remedy {
    margin: 0.45rem 0 0;
    padding: 0.4rem 0.6rem;
    background: var(--surface-alt, #181825);
    border: 1px solid var(--border-color, #45475a);
    border-radius: 4px;
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: 0.74rem;
    color: var(--text-color, #cdd6f4);
    overflow-x: auto;
    user-select: all;
  }

  .containers {
    margin-top: 0.5rem;
    border-top: 1px solid var(--border-color, #45475a);
    padding-top: 0.5rem;
  }

  .containers-toggle {
    background: none;
    border: none;
    color: var(--text-muted, #a6adc8);
    font-size: 0.78rem;
    cursor: pointer;
    padding: 0.15rem 0;
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .containers-toggle:hover {
    color: var(--text-color, #cdd6f4);
  }
  .chev {
    font-size: 0.7rem;
  }

  .container-table {
    width: 100%;
    margin-top: 0.4rem;
    border-collapse: collapse;
    font-size: 0.74rem;
  }
  .container-table th {
    text-align: left;
    font-weight: 500;
    color: var(--text-muted, #6c7086);
    padding: 0.2rem 0.4rem;
    border-bottom: 1px solid var(--border-color, #45475a);
  }
  .container-table td {
    padding: 0.22rem 0.4rem;
    color: var(--text-color, #cdd6f4);
    vertical-align: top;
  }
  .container-table .grouped td {
    color: var(--text-muted, #a6adc8);
  }
  .is-oldest td {
    background: rgba(249, 226, 175, 0.08);
  }
  .oldest-tag {
    margin-left: 0.35rem;
    font-size: 0.65rem;
    color: var(--warning-color, #f9e2af);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .img {
    background: none;
    padding: 0;
    color: var(--text-muted, #a6adc8);
  }

  .others {
    margin: 0.5rem 0 0;
    font-size: 0.72rem;
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  .spinner {
    width: 12px;
    height: 12px;
    border: 2px solid var(--border-color, #45475a);
    border-top-color: var(--primary-color, #89b4fa);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    display: inline-block;
    margin-top: 0.2rem;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
