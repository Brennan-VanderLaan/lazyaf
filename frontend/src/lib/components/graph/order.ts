import type { PipelineGraphModel, PipelineStepV2 } from '../../api/types';

/**
 * A stable, readable order for graph steps: entry points first, then each
 * step reachable from them (breadth-first over the edges), then anything
 * orphaned.
 *
 * ONE definition, in one module, because two surfaces need the same answer
 * and they must not drift (R3): the pipeline CARD (`PipelinesPage`) renders
 * "N steps" plus the first few names, and the debug re-run modal
 * (`stores/debug.ts::debugBreakpointOptions`) renders one checkbox per step.
 * A card that lists a pipeline's last step first, or a checkbox list that
 * reshuffles between renders, is unusable - and "whatever order the Record
 * happens to have" is not a contract.
 *
 * This became shared at 12.8 P3: before it, the card read the retired v1
 * `Pipeline.steps` ARRAY, which carried its order in its indices. The graph
 * has no indices, so the order has to be derived, and deriving it twice is
 * how the two surfaces would come to disagree about what step 1 is.
 */
export function graphStepOrder(graph: PipelineGraphModel): string[] {
  const all = Object.keys(graph.steps ?? {});
  const adjacency = new Map<string, string[]>();
  for (const edge of graph.edges ?? []) {
    if (!edge.from_step || !edge.to_step) continue;
    const list = adjacency.get(edge.from_step) ?? [];
    if (!list.includes(edge.to_step)) list.push(edge.to_step);
    adjacency.set(edge.from_step, list);
  }

  const ordered: string[] = [];
  const seen = new Set<string>();
  const queue = [...(graph.entry_points ?? [])].filter((id) => id in (graph.steps ?? {}));

  while (queue.length > 0) {
    const id = queue.shift() as string;
    if (seen.has(id)) continue;
    seen.add(id);
    ordered.push(id);
    for (const next of adjacency.get(id) ?? []) {
      if (!seen.has(next) && next in graph.steps) queue.push(next);
    }
  }

  // Orphans (no path from any entry point) still get a place: a step the UI
  // refuses to show is a step the user cannot see, count or break on.
  for (const id of all) {
    if (!seen.has(id)) ordered.push(id);
  }
  return ordered;
}

/**
 * The graph's steps as a list, in `graphStepOrder`. Convenience for callers
 * that want the step objects rather than their ids.
 */
export function graphStepList(graph: PipelineGraphModel | null | undefined): PipelineStepV2[] {
  if (!graph || !graph.steps) return [];
  return graphStepOrder(graph)
    .map((id) => graph.steps[id])
    .filter((step): step is PipelineStepV2 => Boolean(step));
}
