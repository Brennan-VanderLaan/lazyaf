/**
 * E2E Tests: Graph Pipeline Editor (Graph Creep - Phase 3)
 *
 * Tests the visual node graph pipeline editor:
 * 1. Adding nodes via all methods (toolbar, palette, context menu)
 * 2. Connecting nodes with edges
 * 3. Editing edge conditions
 * 4. Editing step configuration
 * 5. Entry point management
 * 6. Execution visualization
 *
 * Prerequisites: the compose e2e stack (see e2e/README.md). URLs come from
 * BACKEND_URL/FRONTEND_URL env vars (defaults match the e2e profile).
 */

import { test, expect, type Page } from '@playwright/test';
import { BACKEND_URL, createTestRepo, goToPipelinesPage } from './helpers';

// Helper: Wait for the graph editor to be ready
async function waitForGraphEditor(page: Page) {
  await expect(page.locator('.graph-editor')).toBeVisible({ timeout: 3000 });
  await expect(page.locator('.svelte-flow')).toBeVisible({ timeout: 3000 });
}

/**
 * Connect two nodes through the editor's Connect panel.
 *
 * NOT a drag. SvelteFlow's connection drag does not respond to Playwright's
 * synthetic pointer events - the `dragHandle` helper that used to live here
 * moved the mouse in eight steps with waits between them and still never
 * produced an edge, which is why nine specs in this file stood skipped with a
 * "the functionality works manually" note. The editor now ships a real
 * keyboard/menu affordance for the same job (ConnectPanel), so these specs
 * drive the product the way a keyboard user does rather than the way the
 * library wants a mouse to.
 *
 * `condition` omitted means "take whatever default the editor offers", which
 * is what the smart-default specs assert.
 */
async function connectSteps(
  page: Page,
  from: string,
  to: string,
  condition?: 'success' | 'failure' | 'always',
) {
  const badgesBefore = await page.locator('.condition-badge').count();

  await page.click('[data-testid="toolbar-connect"]');
  await expect(page.locator('[data-testid="connect-panel"]')).toBeVisible({ timeout: 2000 });
  await page.selectOption('[data-testid="connect-from"]', { label: from });
  await page.selectOption('[data-testid="connect-to"]', { label: to });
  if (condition) {
    await page.selectOption('[data-testid="connect-condition"]', condition);
  }
  await page.click('[data-testid="connect-confirm"]');

  // The panel closes itself only on success, so its disappearance IS the
  // assertion that the connection was accepted rather than refused.
  await expect(page.locator('[data-testid="connect-panel"]')).toHaveCount(0, { timeout: 2000 });
  await expect(page.locator('.condition-badge')).toHaveCount(badgesBefore + 1, { timeout: 2000 });
}

/** The condition the panel OFFERS for `from`, without accepting it. */
async function offeredCondition(page: Page, from: string): Promise<string> {
  await page.click('[data-testid="toolbar-connect"]');
  await expect(page.locator('[data-testid="connect-panel"]')).toBeVisible({ timeout: 2000 });
  await page.selectOption('[data-testid="connect-from"]', { label: from });
  const value = await page.locator('[data-testid="connect-condition"]').inputValue();
  await page.click('[data-testid="connect-cancel"]');
  await expect(page.locator('[data-testid="connect-panel"]')).toHaveCount(0);
  return value;
}

// Helper: Add a script step via toolbar (includes modal waits)
async function addScriptStep(page: Page, name: string, command: string) {
  // Count existing nodes before adding
  const existingCount = await page.locator('.step-node').count();

  await page.click('.graph-toolbar .add-btn:has-text("Script")');
  await expect(page.locator('.modal')).toBeVisible({ timeout: 2000 });
  await page.fill('#step-name', name);
  await page.fill('#script-command', command);
  await page.click('button:has-text("Add Step")');

  // Wait for new node to appear
  await expect(page.locator('.step-node')).toHaveCount(existingCount + 1, { timeout: 2000 });

  // Wait for graph to stabilize after re-render
  await page.waitForTimeout(300);
}

// `stabilizeGraph` and `getCanvasBounds` were deleted with `dragHandle`.
// Both existed only to make handle-dragging land: one clicked "fit view" and
// waited 300ms before every drag, the other measured the canvas so a drag
// could be aimed at it. Nothing connects by pixel coordinates any more.

// =============================================================================
// Test Suite: Adding Nodes via Toolbar
// =============================================================================

test.describe('Graph Pipeline Editor - Toolbar Node Creation', () => {
  let repo: { id: string; name: string };

  test.beforeEach(async ({ page }) => {
    repo = await createTestRepo(page, 'e2e-graph');
  });

  test('can add a Script node via toolbar', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);

    // Click "New Pipeline" or similar to start editing
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Click the Script button in toolbar
    await page.click('.graph-toolbar .add-btn:has-text("Script")');

    // Step config modal should open first
    await expect(page.locator('.modal')).toBeVisible();
    await expect(page.locator('.modal h2')).toContainText('Add New Step');

    // Fill in the step name and click Add Step
    await page.fill('#step-name', 'My Script Step');
    await page.click('button:has-text("Add Step")');

    // Now the node should appear on the canvas
    await expect(page.locator('.step-node')).toBeVisible({ timeout: 2000 });
  });

  test('can add a Docker node via toolbar', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Click the Docker button in toolbar
    await page.click('.graph-toolbar .add-btn:has-text("Docker")');

    // Modal should show Docker-specific fields
    await expect(page.locator('.modal')).toBeVisible();
    await expect(page.locator('#docker-image')).toBeVisible();
  });

  test('can add an Agent node via toolbar', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Click the Agent button in toolbar
    await page.click('.graph-toolbar .add-btn:has-text("Agent")');

    // Modal should show Agent-specific fields
    await expect(page.locator('.modal')).toBeVisible();
    await expect(page.locator('#agent-runner')).toBeVisible();
    await expect(page.locator('#agent-title')).toBeVisible();
  });
});

// =============================================================================
// Test Suite: Adding Nodes via Sidebar Palette
// =============================================================================

test.describe('Graph Pipeline Editor - Palette Drag and Drop', () => {
  let repo: { id: string; name: string };

  test.beforeEach(async ({ page }) => {
    repo = await createTestRepo(page, 'e2e-graph');
  });

  test('palette shows all node types', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Check palette is visible
    await expect(page.locator('.node-palette')).toBeVisible();

    // Check all types are available
    await expect(page.locator('.palette-item:has-text("Script")')).toBeVisible();
    await expect(page.locator('.palette-item:has-text("Docker")')).toBeVisible();
    await expect(page.locator('.palette-item:has-text("AI Agent")')).toBeVisible();
  });

  test('can drag Script node from palette to canvas', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Get palette item and canvas
    const paletteItem = page.locator('.palette-item:has-text("Script")');
    const canvas = page.locator('.flow-wrapper');

    // Use Playwright's dragTo for proper HTML5 drag events
    await paletteItem.dragTo(canvas);

    // Modal should appear for step configuration
    await expect(page.locator('.modal')).toBeVisible({ timeout: 2000 });
    await expect(page.locator('.modal h2')).toContainText('Add New Step');
  });

  test('palette shows helpful tip text', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Check for tip text
    await expect(page.locator('.node-palette')).toContainText('Drag to canvas');
    await expect(page.locator('.node-palette')).toContainText('Double-click node to edit');
  });
});

// =============================================================================
// Test Suite: Adding Nodes via Context Menu
// =============================================================================

test.describe('Graph Pipeline Editor - Context Menu', () => {
  let repo: { id: string; name: string };

  test.beforeEach(async ({ page }) => {
    repo = await createTestRepo(page, 'e2e-graph');
  });

  test('right-click on canvas shows context menu', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Right-click on canvas
    const canvas = page.locator('.svelte-flow');
    const canvasBox = await canvas.boundingBox();

    if (!canvasBox) throw new Error('Canvas not found');

    await page.mouse.click(
      canvasBox.x + canvasBox.width / 2,
      canvasBox.y + canvasBox.height / 2,
      { button: 'right' }
    );

    // Context menu should appear
    await expect(page.locator('.context-menu')).toBeVisible();
    await expect(page.locator('.context-menu')).toContainText('Add Step');
  });

  test('context menu shows all node types', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Right-click
    const canvas = page.locator('.svelte-flow');
    await canvas.click({ button: 'right', position: { x: 200, y: 200 } });

    // Check all options
    await expect(page.locator('.context-menu .menu-item:has-text("Script")')).toBeVisible();
    await expect(page.locator('.context-menu .menu-item:has-text("Docker")')).toBeVisible();
    await expect(page.locator('.context-menu .menu-item:has-text("Agent")')).toBeVisible();
  });

  test('clicking context menu item adds node at click position', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Right-click
    const canvas = page.locator('.svelte-flow');
    await canvas.click({ button: 'right', position: { x: 300, y: 200 } });

    // Click "Add Script Step"
    await page.click('.context-menu .menu-item:has-text("Script")');

    // Context menu should close
    await expect(page.locator('.context-menu')).not.toBeVisible();

    // Modal or node should appear
    await expect(page.locator('.modal, .step-node').first()).toBeVisible();
  });

  test('pressing Escape closes context menu', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Right-click
    const canvas = page.locator('.svelte-flow');
    await canvas.click({ button: 'right', position: { x: 200, y: 200 } });
    await expect(page.locator('.context-menu')).toBeVisible();

    // Press Escape
    await page.keyboard.press('Escape');

    // Menu should close
    await expect(page.locator('.context-menu')).not.toBeVisible();
  });

  test('clicking outside context menu closes it', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Right-click to open menu
    const canvas = page.locator('.svelte-flow');
    await canvas.click({ button: 'right', position: { x: 200, y: 200 } });
    await expect(page.locator('.context-menu')).toBeVisible();

    // Click on the palette (outside context menu) to close it
    await page.locator('.node-palette').click();

    // Menu should close
    await expect(page.locator('.context-menu')).not.toBeVisible();
  });
});

// =============================================================================
// Test Suite: Node Editing and Configuration
// =============================================================================

test.describe('Graph Pipeline Editor - Node Configuration', () => {
  let repo: { id: string; name: string };

  test.beforeEach(async ({ page }) => {
    repo = await createTestRepo(page, 'e2e-graph');
  });

  test('double-clicking node opens config modal', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Add a node
    await page.click('.graph-toolbar .add-btn:has-text("Script")');

    // Fill and save the modal
    await page.fill('#step-name', 'Build Step');
    await page.fill('#script-command', 'npm run build');
    await page.click('button:has-text("Add Step")');

    // Double-click the node
    await page.locator('.step-node').dblclick();

    // Config modal should open
    await expect(page.locator('.modal h2')).toContainText('Edit Step');
    await expect(page.locator('#step-name')).toHaveValue('Build Step');
  });

  test('node shows collapsed view by default', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Add a node
    await page.click('.graph-toolbar .add-btn:has-text("Script")');
    await page.fill('#step-name', 'Test Step');
    await page.fill('#script-command', 'npm test');
    await page.click('button:has-text("Add Step")');

    // Node should show name but not full details
    const node = page.locator('.step-node');
    await expect(node).toContainText('Test Step');
    await expect(node.locator('.node-details')).not.toBeVisible();
  });

  test('clicking expand button shows node details', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Add a node
    await page.click('.graph-toolbar .add-btn:has-text("Script")');
    await page.fill('#step-name', 'Expand Test');
    await page.fill('#script-command', 'echo hello');
    await page.click('button:has-text("Add Step")');

    // Click expand button
    await page.locator('.step-node .expand-btn').click();

    // Details should be visible
    await expect(page.locator('.step-node .node-details')).toBeVisible();
    await expect(page.locator('.step-node')).toContainText('echo hello');
  });

  test('can change step type in config modal', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Add a script node
    await page.click('.graph-toolbar .add-btn:has-text("Script")');

    // Change to Docker type
    await page.click('.type-btn:has-text("Docker")');

    // Docker fields should appear
    await expect(page.locator('#docker-image')).toBeVisible();
    await expect(page.locator('#script-command')).not.toBeVisible();
  });
});

// =============================================================================
// Test Suite: Edge Connections and Conditions
// =============================================================================

// =============================================================================
// Test Suite: Edge Connections
//
// These five specs stood SKIPPED from the day they were written, under a note
// saying SvelteFlow's connection mechanism does not respond to Playwright's
// mouse events and "the functionality works manually". That was true, and it
// was also five permanently-dark specs on the only pipeline-authoring surface
// the product has - which is how the editor came to be PUTting to a route that
// does not exist without anything noticing. They are driven now through the
// editor's Connect panel, which is a real affordance (it is also the only way
// to draw an edge from a keyboard) rather than a test hook.
// =============================================================================

test.describe('Graph Pipeline Editor - Edge Connections', () => {
  let repo: { id: string; name: string };

  test.beforeEach(async ({ page }) => {
    repo = await createTestRepo(page, 'e2e-graph');
  });

  test('can connect two nodes', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    await addScriptStep(page, 'Step A', 'echo a');
    await addScriptStep(page, 'Step B', 'echo b');

    await connectSteps(page, 'Step A', 'Step B');

    // SVG edge paths are awkward to assert on; the condition badge is the
    // edge's visible identity and is what the picker specs below click.
    await expect(page.locator('.condition-badge')).toBeVisible({ timeout: 2000 });
  });

  test('new edge defaults to success condition', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    await addScriptStep(page, 'Step 1', 'echo 1');
    await addScriptStep(page, 'Step 2', 'echo 2');

    // The panel OFFERS success before anything is confirmed - i.e. the
    // default is the editor's, not something this spec typed in.
    expect(await offeredCondition(page, 'Step 1')).toBe('success');

    await connectSteps(page, 'Step 1', 'Step 2');
    await expect(page.locator('.condition-badge')).toContainText('ok');
  });

  test('clicking edge badge shows condition picker', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    await addScriptStep(page, 'A', 'echo a');
    await addScriptStep(page, 'B', 'echo b');
    await connectSteps(page, 'A', 'B');

    await page.locator('.condition-badge').click();

    await expect(page.locator('.condition-picker')).toBeVisible();
    await expect(page.locator('.condition-picker')).toContainText('On Success');
    await expect(page.locator('.condition-picker')).toContainText('On Failure');
    await expect(page.locator('.condition-picker')).toContainText('Always');
  });

  test('can change edge condition to failure', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    await addScriptStep(page, 'Main', 'npm test');
    await addScriptStep(page, 'Error Handler', 'echo failed');
    await connectSteps(page, 'Main', 'Error Handler');

    // force: the minimap can overlap the picker's lower options.
    await page.locator('.condition-badge').click();
    await page.locator('.picker-option.failure').click({ force: true });

    await expect(page.locator('.condition-badge')).toContainText('err');
  });

  test('smart defaults: second edge from same source defaults to failure', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    await addScriptStep(page, 'Source', 'npm test');
    await addScriptStep(page, 'On Pass', 'echo pass');
    await addScriptStep(page, 'On Fail', 'echo fail');

    await connectSteps(page, 'Source', 'On Pass');
    await expect(page.locator('.condition-badge').first()).toContainText('ok');

    // The rule under test: a source that already has a success edge offers
    // FAILURE next. `defaultConditionFor` is shared with the drag path, so
    // pinning it here pins it for both.
    expect(await offeredCondition(page, 'Source')).toBe('failure');

    await connectSteps(page, 'Source', 'On Fail');
    await expect(page.locator('.condition-badge').nth(1)).toContainText('err');
  });

  test('refuses to connect a step to itself, naming it', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    await addScriptStep(page, 'Solo', 'echo solo');

    await page.click('[data-testid="toolbar-connect"]');
    await page.selectOption('[data-testid="connect-from"]', { label: 'Solo' });
    await page.selectOption('[data-testid="connect-to"]', { label: 'Solo' });
    await page.click('[data-testid="connect-confirm"]');

    // Refused, said why, and stayed open so the author can fix it (R1),
    // rather than writing a self-edge that fails the run later.
    await expect(page.locator('[data-testid="connect-problem"]')).toContainText('Solo');
    await expect(page.locator('[data-testid="connect-panel"]')).toBeVisible();
    await expect(page.locator('.condition-badge')).toHaveCount(0);
  });
});

// =============================================================================
// Test Suite: Entry Points
// =============================================================================

test.describe('Graph Pipeline Editor - Entry Points', () => {
  let repo: { id: string; name: string };

  test.beforeEach(async ({ page }) => {
    repo = await createTestRepo(page, 'e2e-graph');
  });

  test('Start node is always present in new pipeline', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    // Start node should be visible
    await expect(page.locator('.start-node')).toBeVisible();
    await expect(page.locator('.start-node')).toContainText('Start');
  });

  test('connecting Start node to step sets it as entry point', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    await addScriptStep(page, 'First Step', 'echo first');

    // Start has no outcome to branch on, so its edge is unconditional.
    expect(await offeredCondition(page, 'Start')).toBe('always');

    await connectSteps(page, 'Start', 'First Step');
    await expect(page.locator('.condition-badge')).toContainText('->');
  });

  test('Start node can connect to multiple steps for parallel execution', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    await addScriptStep(page, 'Parallel A', 'echo A');
    await addScriptStep(page, 'Parallel B', 'echo B');

    await connectSteps(page, 'Start', 'Parallel A');
    await connectSteps(page, 'Start', 'Parallel B');

    await expect(page.locator('.condition-badge')).toHaveCount(2, { timeout: 2000 });
  });
});

// =============================================================================
// DELETED, NOT SKIPPED: "Graph Pipeline Editor - Execution Visualization".
//
// It was a `test.describe` holding `test.skip(true, 'Requires backend pipeline
// run support')` and a comment listing four things it "would verify" - and
// ZERO tests. It could never run, never fail, and nothing could ever un-skip
// it, so it measured nothing while looking, in a skip count, exactly like
// coverage that was temporarily parked (R4).
//
// The behaviour it named is not uncovered: node status colours, active-step
// pulsing and edge animation are driven from `stepStatuses` / `activeStepIds`
// / `completedStepIds`, and a real run through them is exercised by
// tdd/e2e/test_graph_pipeline.py and by the dogfood pipeline. A UI-level spec
// for it needs a live run to watch, which is `dogfood-live.spec.ts`'s
// territory, not a placeholder here.
// =============================================================================

// =============================================================================
// Test Suite: Saving and Loading
// =============================================================================

test.describe('Graph Pipeline Editor - Save and Load', () => {
  let repo: { id: string; name: string };

  test.beforeEach(async ({ page }) => {
    repo = await createTestRepo(page, 'e2e-graph');
  });

  test('saves a NEW pipeline and then UPDATES it, through the editor', async ({ page }) => {
    await goToPipelinesPage(page, repo.name);
    await page.click('button:has-text("New Pipeline")');
    await waitForGraphEditor(page);

    await page.fill('input[placeholder*="Pipeline name"]', 'My Graph Pipeline');
    await addScriptStep(page, 'Build', 'npm build');

    // An entry point is what makes a pipeline runnable and the ONLY way to
    // declare one is an edge from Start. Until the Connect panel existed
    // this spec could not get past this line, which is why it was skipped.
    await connectSteps(page, 'Start', 'Build');

    await page.click('[data-testid="save-pipeline"]');
    await expect(page.locator('[data-testid="pipelines-page"]')).toBeVisible({ timeout: 5000 });

    const listed = await page.request.get(`${BACKEND_URL}/api/repos/${repo.id}/pipelines`);
    expect(listed.ok()).toBeTruthy();
    const pipelines = await listed.json();
    const created = pipelines.find((p: any) => p.name === 'My Graph Pipeline');
    expect(created, 'the pipeline the editor just saved').toBeTruthy();

    // THE POINT OF THE SPEC: what was persisted, read back from the API.
    const graph = created.steps_graph;
    expect(graph, 'steps_graph').toBeTruthy();
    const buildId = Object.keys(graph.steps).find((id: string) => graph.steps[id].name === 'Build');
    expect(buildId).toBeTruthy();
    expect(graph.entry_points).toEqual([buildId]);
    expect(graph.steps[buildId!].config.command).toBe('npm build');

    // ---- and now UPDATE it. This is the half nothing has ever covered: the
    // editor PUT to a route that does not exist, so saving an EXISTING
    // pipeline 405'd every time and no test looked. ----
    await page.locator('.pipeline-card:has-text("My Graph Pipeline") button:has-text("Edit")').click();
    await waitForGraphEditor(page);
    await expect(page.locator('.step-node')).toContainText('Build');

    await addScriptStep(page, 'Test', 'npm test');
    await connectSteps(page, 'Build', 'Test');
    await page.click('[data-testid="save-pipeline"]');
    await expect(page.locator('[data-testid="pipelines-page"]')).toBeVisible({ timeout: 5000 });

    const reread = await page.request.get(`${BACKEND_URL}/api/pipelines/${created.id}`);
    expect(reread.ok(), 'GET the pipeline the editor just updated').toBeTruthy();
    const updated = await reread.json();
    const updatedGraph = updated.steps_graph;

    const ids = Object.keys(updatedGraph.steps);
    const byName: Record<string, string> = {};
    for (const id of ids) byName[updatedGraph.steps[id].name] = id;
    expect(Object.keys(byName).sort()).toEqual(['Build', 'Test']);

    expect(updatedGraph.entry_points).toEqual([byName['Build']]);
    expect(updatedGraph.edges).toContainEqual(
      expect.objectContaining({
        from_step: byName['Build'],
        to_step: byName['Test'],
        condition: 'success',
      }),
    );
  });

  test('saved pipeline loads with graph structure intact', async ({ page }) => {
    // Create pipeline via API with graph structure
    const graphData = {
      steps: {
        step_1: {
          id: 'step_1',
          name: 'Saved Step',
          type: 'script',
          config: { command: 'echo saved' },
          position: { x: 150, y: 100 },
          timeout: 300,
        },
      },
      edges: [],
      entry_points: ['step_1'],
      version: 2,
    };

    await page.request.post(`${BACKEND_URL}/api/repos/${repo.id}/pipelines`, {
      data: {
        name: 'Saved Pipeline',
        steps_graph: graphData,
      },
    });

    // Go to pipelines page
    await goToPipelinesPage(page, repo.name);

    // Click on the saved pipeline to edit (click the Edit button within the card)
    await page.locator('.pipeline-card:has-text("Saved Pipeline") button:has-text("Edit")').click();

    // Wait for graph editor
    await waitForGraphEditor(page);

    // Node should be visible with correct name
    await expect(page.locator('.step-node')).toContainText('Saved Step');
  });
});
