/**
 * Turning a thrown thing into a sentence a human can act on.
 *
 * QA triage T7: "Every failure is a native `alert('Unknown error')`". The
 * source was one line — `api/client.ts` substituted the literal string
 * "Unknown error" whenever an error body was not JSON, which covered a
 * plain-text 500, a proxy's HTML 502 and an unreachable backend alike. Roughly
 * seventy `catch` blocks then rendered that string verbatim. A user hitting it
 * learned nothing: not what failed, not whether retrying would help, not
 * whether the backend was even running.
 *
 * `client.ts` now throws `ApiError` carrying the real status and the server's
 * own detail. This module is the other half: whatever reaches a catch block,
 * say the most specific true thing about it, and never invent reassurance
 * (standing rule R1).
 */
import { ApiError } from '../api/client';

/**
 * The most specific description available for a caught value.
 *
 * `fallback` is used only when the thrown value carries no message at all -
 * it names the operation ("Failed to start card"), so even the worst case is
 * more informative than "Unknown error".
 */
export function describeError(error: unknown, fallback = 'The request failed'): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message.trim() !== '') return error.message;
  if (typeof error === 'string' && error.trim() !== '') return error;
  return fallback;
}

/**
 * True when the failure was "the backend is not there" rather than "the
 * backend said no". The distinction is the difference between a retry that
 * might work and a request that will keep being rejected.
 */
export function isUnreachable(error: unknown): boolean {
  return error instanceof ApiError && error.isNetworkError;
}
