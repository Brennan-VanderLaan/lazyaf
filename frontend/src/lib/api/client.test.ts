import { describe, it, expect, beforeEach, vi } from 'vitest';
import { repos, cards, pipelines, pipelineRuns, lazyafFiles, playground, runners } from './client';

// Capture the request the client builds instead of hitting the network.
const fetchMock = vi.fn();
vi.stubGlobal('fetch', fetchMock);

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
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

  it('builds the runner docker-command query', async () => {
    await runners.dockerCommand('gemini', true);
    expect(lastRequest().url).toBe('/api/runners/docker-command?runner_type=gemini&with_secrets=true');
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

describe('response handling', () => {
  it('throws the server-provided detail on error responses', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Card not found' }, 404));
    await expect(cards.get('nope')).rejects.toThrow('Card not found');
  });

  it('falls back to the HTTP status when the error body has no detail', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}, 500));
    await expect(repos.list()).rejects.toThrow('HTTP 500');
  });

  it('reports "Unknown error" when the error body is not JSON at all', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => { throw new Error('not json'); },
    });
    await expect(repos.list()).rejects.toThrow('Unknown error');
  });

  it('returns undefined for 204 No Content', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 204,
      json: async () => { throw new Error('no body'); },
    });
    await expect(cards.delete('c1')).resolves.toBeUndefined();
  });

  it('returns the parsed JSON body on success', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([{ id: 'r1' }]));
    await expect(repos.list()).resolves.toEqual([{ id: 'r1' }]);
  });
});
