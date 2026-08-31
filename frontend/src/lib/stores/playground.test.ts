import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import {
  playgroundStore,
  isRunning,
  canStart,
  hasResult,
  attachmentGate,
  limitsSentence,
  formatBytes,
} from './playground';
import type { PlaygroundModalitySupport } from './playground';
import type { PlaygroundStatus } from '../api/types';

beforeEach(() => {
  playgroundStore.reset();
});

describe('playgroundStore.setConfig', () => {
  it('merges partial config without touching other fields', () => {
    playgroundStore.setConfig({ repoId: 'repo-1', taskOverride: 'do the thing' });

    const state = get(playgroundStore);
    expect(state.repoId).toBe('repo-1');
    expect(state.taskOverride).toBe('do the thing');
    // Untouched defaults survive
    expect(state.runnerType).toBe('claude-code');
    expect(state.status).toBe('idle');
  });
});

describe('playgroundStore.clearLogs / reset', () => {
  it('clearLogs empties logs but keeps configuration', () => {
    playgroundStore.setConfig({ branch: 'main', logs: ['line 1', 'line 2'] });
    playgroundStore.clearLogs();

    const state = get(playgroundStore);
    expect(state.logs).toEqual([]);
    expect(state.branch).toBe('main');
  });

  it('reset restores the initial state', () => {
    playgroundStore.setConfig({
      repoId: 'repo-1',
      status: 'failed' as PlaygroundStatus,
      error: 'oops',
      logs: ['x'],
    });
    playgroundStore.reset();

    const state = get(playgroundStore);
    expect(state.repoId).toBeNull();
    expect(state.status).toBe('idle');
    expect(state.error).toBeNull();
    expect(state.logs).toEqual([]);
  });
});

describe('derived: isRunning / canStart', () => {
  it('isRunning is true only for queued and running', () => {
    const expectations: Array<[PlaygroundStatus, boolean]> = [
      ['idle', false],
      ['queued', true],
      ['running', true],
      ['completed', false],
      ['failed', false],
      ['cancelled', false],
    ];
    for (const [status, expected] of expectations) {
      playgroundStore.setConfig({ status });
      expect(get(isRunning), `status=${status}`).toBe(expected);
    }
  });

  it('canStart is the complement of an in-flight session', () => {
    playgroundStore.setConfig({ status: 'idle' });
    expect(get(canStart)).toBe(true);

    playgroundStore.setConfig({ status: 'running' });
    expect(get(canStart)).toBe(false);

    playgroundStore.setConfig({ status: 'cancelled' });
    expect(get(canStart)).toBe(true);
  });
});

describe('derived: hasResult', () => {
  it('is false while running even with a diff present', () => {
    playgroundStore.setConfig({ status: 'running', diff: 'diff --git a b' });
    expect(get(hasResult)).toBe(false);
  });

  /**
   * CHANGED DELIBERATELY (QA-7, finding PG-07). This used to assert `false`,
   * which is what made the single most natural first prompt a stranger types
   * ("what does this repo do?") finish with no Changes section, no "nothing
   * changed" message and no Reset button - the page looked like nothing had
   * happened. `hasResult` now means "the run reached a terminal state", not
   * "the run produced a diff", and the page renders the empty-changes branch.
   */
  it('is true when completed with no diff, error, or changed files', () => {
    playgroundStore.setConfig({ status: 'completed', diff: null, error: null, filesChanged: [] });
    expect(get(hasResult)).toBe(true);
  });

  it('is true when a run was cancelled', () => {
    playgroundStore.setConfig({ status: 'cancelled', diff: null, error: null, filesChanged: [] });
    expect(get(hasResult)).toBe(true);
  });

  it('is true when completed with a diff', () => {
    playgroundStore.setConfig({ status: 'completed', diff: 'diff --git a b' });
    expect(get(hasResult)).toBe(true);
  });

  it('is true when failed with an error', () => {
    playgroundStore.setConfig({ status: 'failed', error: 'agent exploded' });
    expect(get(hasResult)).toBe(true);
  });

  it('is true when completed with only files changed', () => {
    playgroundStore.setConfig({ status: 'completed', filesChanged: ['a.ts'] });
    expect(get(hasResult)).toBe(true);
  });
});

// ===========================================================================
// Attachments: the gate that decides whether a human may attach (14.5)
// ===========================================================================
//
// EVERY CASE HERE IS A DISABLED CONTROL WITH A DIFFERENT SENTENCE, and that is
// the point. The failure this feature exists to prevent is a file accepted and
// silently never delivered, so an optimistic default anywhere in this chain is
// the bug. The two states that must never read alike are `unprobed` ("nobody
// asked - probe it") and `undetectable` ("the server took the image, returned
// 200, and the prompt token count did not move").

/** A ModelEndpoint shaped enough for the gate. Only the fields it reads. */
function endpointWith(modalities: unknown, name = 'local-4090'): any {
  return {
    id: 'ep-1',
    name,
    model: 'qwen2.5-vl',
    enabled: true,
    health: 'healthy',
    capabilities: {
      supports_tools: true,
      supports_streaming: true,
      reports_usage: true,
      context_window: 32768,
      max_output_tokens: null,
      probe_status: 'ok',
      probed_at: '2026-08-31T00:00:00',
      probed_from: 'backend',
      probe_age_seconds: 10,
      stale: false,
      ...(modalities === undefined ? {} : { modalities }),
    },
  };
}

function modality(state: string, extra: Record<string, unknown> = {}) {
  return {
    modality: 'images',
    state,
    source: 'wire_probe',
    reason: null,
    evidence: null,
    caveat: null,
    ...extra,
  };
}

const CARRIED: PlaygroundModalitySupport = {
  modality: 'images',
  attachable: true,
  reason: 'the harness can carry it',
};
const NOT_CARRIED: PlaygroundModalitySupport = {
  modality: 'images',
  attachable: false,
  reason: 'LazyAF cannot yet deliver an attachment to a model.',
};

describe('attachmentGate', () => {
  it('refuses for a CLI runner without blaming the endpoint', () => {
    // Claude Code and Gemini have no capability record to render. Saying
    // "this endpoint cannot see images" would answer a question nobody asked
    // and would be false about the CLI.
    const gate = attachmentGate({
      runnerType: 'claude-code',
      endpoint: endpointWith([modality('supported')]),
      platform: CARRIED,
    });
    expect(gate.enabled).toBe(false);
    expect(gate.blockedBy).toBe('runner');
    expect(gate.state).toBeNull();
    expect(gate.reason).toMatch(/CLI agent/);
  });

  it('refuses with nothing selected, and says what to pick', () => {
    const gate = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: null,
      platform: CARRIED,
    });
    expect(gate.enabled).toBe(false);
    expect(gate.blockedBy).toBe('no-endpoint');
    expect(gate.next).toMatch(/endpoint/);
  });

  it('treats a backend with NO modalities list as its own fourth unknown', () => {
    // Not `unprobed`: pressing Probe cannot fix a backend that has no way to
    // answer, so offering Probe would be a loop that never terminates.
    const gate = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: endpointWith(undefined),
      platform: CARRIED,
    });
    expect(gate.enabled).toBe(false);
    expect(gate.blockedBy).toBe('unreported');
    expect(gate.next).toBeNull();
    expect(gate.state).toBeNull();
  });

  it('refuses when the list is present but says nothing about images', () => {
    const gate = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: endpointWith([{ ...modality('supported'), modality: 'audio' }]),
      platform: CARRIED,
    });
    expect(gate.enabled).toBe(false);
    expect(gate.blockedBy).toBe('unreported');
    expect(gate.reason).toMatch(/absence, not a "no"/);
  });

  it.each([
    ['unprobed'],
    ['unsupported'],
    ['probe_failed'],
    ['undetectable'],
    ['unrepresentable'],
  ])('refuses on endpoint state %s and reports that exact state', (state) => {
    const gate = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: endpointWith([modality(state)]),
      platform: CARRIED,
    });
    expect(gate.enabled).toBe(false);
    expect(gate.blockedBy).toBe('endpoint');
    expect(gate.state).toBe(state);
    expect(gate.reason.length).toBeGreaterThan(0);
  });

  it('gives unprobed and undetectable DIFFERENT sentences', () => {
    // The collapse that would be a lie. `unprobed` says "press Probe";
    // `undetectable` says the request succeeded and the image went nowhere.
    // They both disable, and they lead to different actions.
    const unprobed = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: endpointWith([modality('unprobed')]),
      platform: CARRIED,
    });
    const undetectable = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: endpointWith([modality('undetectable')]),
      platform: CARRIED,
    });
    expect(unprobed.reason).not.toBe(undetectable.reason);
    expect(unprobed.next).not.toBeNull();
  });

  it('puts the ENDPOINT reason before the platform one', () => {
    // Ordering is the design: telling someone "LazyAF cannot send images"
    // when their endpoint was never probed points them at the wrong fix, and
    // the platform answer is identical for every endpoint anyway.
    const gate = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: endpointWith([modality('unprobed')]),
      platform: NOT_CARRIED,
    });
    expect(gate.blockedBy).toBe('endpoint');
  });

  it('refuses when the endpoint can see but the platform cannot carry', () => {
    const gate = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: endpointWith([modality('supported')]),
      platform: NOT_CARRIED,
    });
    expect(gate.enabled).toBe(false);
    expect(gate.blockedBy).toBe('platform');
    expect(gate.state).toBe('supported');
    expect(gate.reason).toBe(NOT_CARRIED.reason);
  });

  it('FAILS CLOSED when the capability read itself failed', () => {
    // "We could not ask" is not "yes". This is the single most important
    // default in the file.
    const gate = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: endpointWith([modality('supported')]),
      platform: null,
    });
    expect(gate.enabled).toBe(false);
    expect(gate.blockedBy).toBe('platform');
    expect(gate.reason).toMatch(/Unknown is not yes/);
  });

  it('enables only when the endpoint AND the platform both say yes', () => {
    const gate = attachmentGate({
      runnerType: 'openai-harness',
      endpoint: endpointWith([modality('supported')]),
      platform: CARRIED,
    });
    expect(gate.enabled).toBe(true);
    expect(gate.blockedBy).toBeNull();
  });

  it('always produces a reason, in every state', () => {
    // A control greyed for a reason nobody wrote down is the bug this whole
    // section exists to avoid, so the invariant is asserted rather than
    // assumed.
    const cases = [
      { runnerType: 'gemini', endpoint: null, platform: null },
      { runnerType: 'openai-harness', endpoint: null, platform: CARRIED },
      { runnerType: 'openai-harness', endpoint: endpointWith(undefined), platform: CARRIED },
      { runnerType: 'openai-harness', endpoint: endpointWith([modality('unprobed')]), platform: CARRIED },
      { runnerType: 'openai-harness', endpoint: endpointWith([modality('supported')]), platform: NOT_CARRIED },
      { runnerType: 'openai-harness', endpoint: endpointWith([modality('supported')]), platform: CARRIED },
    ];
    for (const c of cases) {
      expect(attachmentGate(c as any).reason.trim().length).toBeGreaterThan(0);
    }
  });
});

describe('limitsSentence / formatBytes', () => {
  it('states every cap the server declared', () => {
    const sentence = limitsSentence({
      max_files: 4,
      max_bytes_per_file: 5 * 1024 * 1024,
      max_bytes_total: 8 * 1024 * 1024,
      media_types: ['image/png', 'image/jpeg'],
    });
    expect(sentence).toContain('4 files');
    expect(sentence).toContain('5 MiB');
    expect(sentence).toContain('8 MiB');
    expect(sentence).toContain('PNG');
    expect(sentence).toContain('JPEG');
  });

  it('says the limits are UNKNOWN rather than inventing them', () => {
    // Printing a plausible default here is how a UI ends up telling someone a
    // limit the validator does not enforce.
    expect(limitsSentence(null)).toMatch(/unknown/i);
    expect(limitsSentence(null)).not.toMatch(/\d+ MiB/);
  });

  it('formats sub-megabyte and fractional sizes without lying', () => {
    expect(formatBytes(1024)).toBe('1 KiB');
    expect(formatBytes(1536 * 1024)).toBe('1.5 MiB');
    expect(formatBytes(-1)).toBe('unknown');
  });
});

describe('playgroundStore.reset and the capability read', () => {
  it('keeps capabilities across Reset', () => {
    // They are a property of the BUILD, not of the run. Dropping them would
    // leave the attach control unable to state its limits until a second
    // round trip landed.
    const capabilities = {
      attachment_limits: {
        max_files: 4,
        max_bytes_per_file: 1,
        max_bytes_total: 2,
        media_types: ['image/png'],
      },
      modalities: [NOT_CARRIED],
    };
    playgroundStore.setConfig({ capabilities, repoId: 'repo-1' });
    playgroundStore.reset();

    const state = get(playgroundStore);
    expect(state.repoId).toBeNull();
    expect(state.capabilities).toEqual(capabilities);
  });
});
