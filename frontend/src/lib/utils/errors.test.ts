/**
 * QA triage T7: "Every failure is a native `alert('Unknown error')`".
 *
 * These pin the replacement rule: say the most specific true thing about the
 * failure, and distinguish "the backend refused" from "the backend is not
 * there" — because only one of those is worth retrying.
 */
import { describe, it, expect } from 'vitest';

import { ApiError, NETWORK_ERROR_STATUS } from '../api/client';
import { describeError, isUnreachable } from './errors';

describe('describeError', () => {
  it('uses the server sentence carried by an ApiError', () => {
    const error = new ApiError(409, 'Card is not in review (HTTP 409 Conflict)', 'Card is not in review');
    expect(describeError(error)).toBe('Card is not in review (HTTP 409 Conflict)');
  });

  it('uses a plain Error message', () => {
    expect(describeError(new Error('boom'))).toBe('boom');
  });

  it('uses a thrown string', () => {
    expect(describeError('something went sideways')).toBe('something went sideways');
  });

  it('names the operation instead of saying "Unknown error" for a bare throw', () => {
    expect(describeError(null, 'Failed to start card')).toBe('Failed to start card');
    expect(describeError(undefined, 'Failed to start card')).toBe('Failed to start card');
    expect(describeError({}, 'Failed to start card')).toBe('Failed to start card');
    expect(describeError(new Error('   '), 'Failed to start card')).toBe('Failed to start card');
    expect(describeError('  ', 'Failed to start card')).toBe('Failed to start card');
  });

  it('never returns an empty string', () => {
    for (const thrown of [null, undefined, '', '   ', 0, new Error('')]) {
      expect(describeError(thrown).length).toBeGreaterThan(0);
    }
  });
});

describe('isUnreachable', () => {
  it('is true only for a request that never reached a server', () => {
    expect(isUnreachable(new ApiError(NETWORK_ERROR_STATUS, 'Cannot reach the LazyAF backend'))).toBe(true);
  });

  it('is false for a server that answered with a failure', () => {
    expect(isUnreachable(new ApiError(500, 'HTTP 500'))).toBe(false);
    expect(isUnreachable(new ApiError(404, 'HTTP 404'))).toBe(false);
    expect(isUnreachable(new Error('Failed to fetch'))).toBe(false);
    expect(isUnreachable(null)).toBe(false);
  });
});
