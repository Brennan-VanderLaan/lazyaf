import type { EdgeCondition } from '../../api/types';

/**
 * Handlers a graph child (an edge, a node) needs from the editor that owns the
 * graph.
 *
 * These used to be passed as closures inside each Svelte Flow `edge.data`
 * object. That is the shape Svelte Flow explicitly does not want: it probes
 * `structuredClone(edges[0])` to guess whether the array is a deep `$state`
 * proxy, a function in `data` makes that throw, and the library reports it to
 * the console as "Use $state.raw for edges to prevent performance issues." —
 * the only warning the app produced on any route.
 *
 * Passing them through context instead keeps `data` serialisable (what the
 * library documents) and silences the warning by removing its actual cause
 * rather than by muting it.
 */
export interface GraphActions {
  /** Change an edge's condition. Accepts synthetic `__start_to_<step>` ids. */
  setEdgeCondition(edgeId: string, condition: EdgeCondition): void;
  /** Delete an edge. Accepts synthetic `__start_to_<step>` ids. */
  deleteEdge(edgeId: string): void;
}

export const GRAPH_ACTIONS = Symbol('lazyaf.graph.actions');
