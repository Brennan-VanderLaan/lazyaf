import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  ApiError,
  NETWORK_ERROR_STATUS,
  repos,
  cards,
  pipelines,
  pipelineRuns,
  lazyafFiles,
  playground,
  runners,
} from './client';

// Capture the request the client builds instead of hitting the network.
const fetchMock = vi.fn();
vi.stubGlobal('fetch', fetchMock);

/**
 * A stand-in Response. `text()` is the ONLY body reader the error path uses
 * (a real body can be consumed once), so the fakes model that: `json()` is
 * for the success path, `text()` for the failure path, and neither is called
 * twice.
 */
function jsonResponse(body: unknown, status = 200, statusText = '') {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function rawResponse(text: string, status: number, statusText = '') {
  return {
    ok: false,
    status,
    statusText,
    json: async () => {
      throw new Error('not json');
    },
    text: async () => text,
  };
}

function lastRequest(): { url: string; options: RequestInit } {
  const call = fetchMock.mock.calls.at(-1);
  if (!call) throw new Error('fetch was not called');
  return { url: call[0] as string, options: (call[1] ?? {}) as RequestInit };
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(jsonResponse({}));
});

describe('URL building', () => {
  it('prefixes every request with /api', async () => {
    await repos.list();
    expect(lastRequest().url).toBe('/api/repos');
  });

  it('encodes branch names in delete-branch paths', async () => {
    await repos.deleteBranch('r1', 'feature/with slash');
    const { url, options } = lastRequest();
    expect(url).toBe('/api/repos/r1/branches/feature%2Fwith%20slash');
    expect(options.method).toBe('DELETE');
  });

  it('builds commits query params from branch and limit', async () => {
    await repos.commits('r1', 'dev', 5);
    expect(lastRequest().url).toBe('/api/repos/r1/commits?branch=dev&limit=5');
  });

  it('omits the branch param when not provided', async () => {
    await repos.commits('r1');
    expect(lastRequest().url).toBe('/api/repos/r1/commits?limit=20');
  });

  it('encodes both refs in diff URLs', async () => {
    await repos.diff('r1', 'main', 'card/my branch');
    expect(lastRequest().url).toBe('/api/repos/r1/diff?base=main&head=card%2Fmy%20branch');
  });

  it('filters pipeline runs via query params', async () => {
    await pipelineRuns.list({ pipeline_id: 'p1', status: 'running', limit: 3 });
    expect(lastRequest().url).toBe('/api/pipeline-runs?pipeline_id=p1&status=running&limit=3');
  });

  it('encodes repo-defined pipeline names and branch', async () => {
    await lazyafFiles.getPipeline('r1', 'ci build', 'feat/x');
    expect(lastRequest().url).toBe('/api/repos/r1/lazyaf/pipelines/ci%20build?branch=feat%2Fx');
  });

  // 12.6: the runners API is READ-ONLY over the registry. `docker-command`
  // (and register/heartbeat/job/logs) served the polling pool and left with
  // it; `list()` is the snapshot half of the panel's snapshot-then-delta
  // model, so it is the whole client surface now.
  it('fetches the runner snapshot from the read-only runners route', async () => {
    await runners.list();
    expect(lastRequest().url).toBe('/api/runners');
  });

  it('streamUrl is a plain string for EventSource use', () => {
    expect(playground.streamUrl('sess-1')).toBe('/api/playground/sess-1/stream');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('request bodies and methods', () => {
  it('serializes card approve payloads with a null default target', async () => {
    await cards.approve('c1');
    const { url, options } = lastRequest();
    expect(url).toBe('/api/cards/c1/approve');
    expect(options.method).toBe('POST');
    expect(JSON.parse(options.body as string)).toEqual({ target_branch: null });
  });

  it('passes an explicit target branch through', async () => {
    await cards.approve('c1', 'release');
    expect(JSON.parse(lastRequest().options.body as string)).toEqual({ target_branch: 'release' });
  });

  it('defaults pipeline run creation to an empty object body', async () => {
    await pipelines.run('p1');
    expect(JSON.parse(lastRequest().options.body as string)).toEqual({});
  });

  it('always sends the JSON content-type header', async () => {
    await cards.get('c1');
    const headers = lastRequest().options.headers as Record<string, string>;
    expect(headers['Content-Type']).toBe('application/json');
  });
});

/**
 * QA triage T7: every failure used to reach the user as
 * `alert("Unknown error")`. The status code was discarded before anything
 * could use it, FastAPI's 422 array rendered as `[object Object]`, a
 * plain-text 500 or an HTML 502 became the literal words "Unknown error", and
 * an unreachable backend surfaced as a bare `TypeError: Failed to fetch`.
 *
 * These pin the replacement: an `ApiError` that keeps the status and always
 * carries text a human can act on.
 */
describe('response handling', () => {
  it('throws the server-provided detail on error responses', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Card not found' }, 404, 'Not Found'));
    await expect(cards.get('nope')).rejects.toThrow('Card not found');
  });

  it('keeps the HTTP status on the thrown error instead of discarding it', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Card not found' }, 404, 'Not Found'));
    const error = await cards.get('nope').catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(404);
    expect(error.detail).toBe('Card not found');
    expect(error.isNetworkError).toBe(false);
  });

  it('falls back to the HTTP status when the error body has no detail', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}, 500, 'Internal Server Error'));
    await expect(repos.list()).rejects.toThrow('HTTP 500');
  });

  it('flattens a FastAPI 422 array instead of rendering [object Object]', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { detail: [{ loc: ['body', 'name'], msg: 'Field required', type: 'missing' }] },
        422,
        'Unprocessable Entity',
      ),
    );
    const error = await repos.create({ name: '' } as never).catch((e) => e);
    expect(error.message).toContain('name: Field required');
    expect(error.message).not.toContain('[object Object]');
    expect(error.status).toBe(422);
  });

  it('shows a non-JSON error body verbatim rather than saying "Unknown error"', async () => {
    // A plain-text 500 from an unhandled backend exception (QA triage T3).
    fetchMock.mockResolvedValueOnce(rawResponse('Internal Server Error', 500, 'Internal Server Error'));
    const error = await repos.list().catch((e) => e);
    expect(error.message).toContain('Internal Server Error');
    expect(error.message).toContain('HTTP 500');
    expect(error.message).not.toContain('Unknown error');
  });

  it('truncates a huge HTML error body instead of pasting a page into an alert', async () => {
    fetchMock.mockResolvedValueOnce(rawResponse('<html>' + 'x'.repeat(5000) + '</html>', 502, 'Bad Gateway'));
    const error = await repos.list().catch((e) => e);
    expect(error.status).toBe(502);
    expect(error.message.length).toBeLessThan(300);
    expect(error.message).toContain('…');
  });

  it('reports an unreachable backend as such, with status 0', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const error = await repos.list().catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(NETWORK_ERROR_STATUS);
    expect(error.isNetworkError).toBe(true);
    expect(error.message).toContain('Cannot reach the LazyAF backend');
  });

  it('returns undefined for 204 No Content', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 204,
      statusText: 'No Content',
      json: async () => { throw new Error('no body'); },
      text: async () => '',
    });
    await expect(cards.delete('c1')).resolves.toBeUndefined();
  });

  it('returns the parsed JSON body on success', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([{ id: 'r1' }]));
    await expect(repos.list()).resolves.toEqual([{ id: 'r1' }]);
  });
});
