import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';

vi.mock('../api/client', () => ({
  features: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    stories: vi.fn(),
    seedMilestone12: vi.fn(),
  },
  userStories: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    criteria: vi.fn(),
  },
  criteria: {
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
  },
}));

import { specStore, featureStoryCounts } from './spec';
import {
  features as featuresApi,
  userStories as storiesApi,
  criteria as criteriaApi,
} from '../api/client';
import type { Feature, UserStory, AcceptanceCriterion } from '../api/types';

function makeFeature(overrides: Partial<Feature> = {}): Feature {
  return {
    id: 'f1',
    title: 'Feature One',
    description: '',
    status: 'draft',
    repo_ids: [],
    ...overrides,
  };
}

function makeStory(overrides: Partial<UserStory> = {}): UserStory {
  return {
    id: 's1',
    feature_id: 'f1',
    title: 'Story One',
    narrative: '',
    status: 'draft',
    priority: null,
    ...overrides,
  };
}

function makeCriterion(overrides: Partial<AcceptanceCriterion> = {}): AcceptanceCriterion {
  return {
    id: 'c1',
    user_story_id: 's1',
    text: 'It works',
    required: true,
    notes: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  specStore.clear();
});

describe('specStore.loadAll', () => {
  it('loads features with their stories eagerly so counts are available', async () => {
    const f1 = makeFeature({ id: 'f1' });
    const f2 = makeFeature({ id: 'f2', title: 'Feature Two' });
    vi.mocked(featuresApi.list).mockResolvedValue([f1, f2]);
    vi.mocked(storiesApi.list).mockResolvedValue([
      makeStory({ id: 's1', feature_id: 'f1' }),
      makeStory({ id: 's2', feature_id: 'f1' }),
    ]);

    await specStore.loadAll();

    const state = get(specStore);
    expect(state.features).toHaveLength(2);
    expect(state.storiesByFeature['f1']).toHaveLength(2);
    expect(state.storiesByFeature['f2']).toEqual([]);
    expect(state.loading).toBe(false);
    expect(state.error).toBeNull();

    const counts = get(featureStoryCounts);
    expect(counts['f1']).toBe(2);
    expect(counts['f2']).toBe(0);
  });

  it('fetches exactly 2 requests regardless of feature count (no 1+N)', async () => {
    vi.mocked(featuresApi.list).mockResolvedValue([
      makeFeature({ id: 'f1' }),
      makeFeature({ id: 'f2' }),
      makeFeature({ id: 'f3' }),
    ]);
    vi.mocked(storiesApi.list).mockResolvedValue([makeStory({ id: 's1', feature_id: 'f2' })]);

    await specStore.loadAll();

    expect(featuresApi.list).toHaveBeenCalledTimes(1);
    expect(storiesApi.list).toHaveBeenCalledTimes(1);
    // The unfiltered listing: no per-feature story fetches at all.
    expect(storiesApi.list).toHaveBeenCalledWith();
    expect(featuresApi.stories).not.toHaveBeenCalled();
    expect(get(specStore).storiesByFeature['f2'].map(s => s.id)).toEqual(['s1']);
  });

  it('drops stories whose feature is unknown instead of crashing', async () => {
    vi.mocked(featuresApi.list).mockResolvedValue([makeFeature({ id: 'f1' })]);
    vi.mocked(storiesApi.list).mockResolvedValue([
      makeStory({ id: 's1', feature_id: 'f1' }),
      makeStory({ id: 'orphan', feature_id: 'gone' }),
    ]);

    await specStore.loadAll();

    const state = get(specStore);
    expect(state.error).toBeNull();
    expect(state.storiesByFeature['f1'].map(s => s.id)).toEqual(['s1']);
    expect('gone' in state.storiesByFeature).toBe(false);
  });

  it('records the error and clears loading on failure', async () => {
    vi.mocked(featuresApi.list).mockRejectedValue(new Error('backend down'));

    await specStore.loadAll();

    const state = get(specStore);
    expect(state.error).toBe('backend down');
    expect(state.loading).toBe(false);
    expect(state.features).toEqual([]);
  });

  it('preserves expansion state across reloads', async () => {
    vi.mocked(featuresApi.list).mockResolvedValue([makeFeature()]);
    vi.mocked(storiesApi.list).mockResolvedValue([makeStory()]);

    await specStore.loadAll();
    specStore.toggleFeature('f1');
    await specStore.loadAll();

    expect(get(specStore).expandedFeatureIds).toContain('f1');
  });
});

describe('specStore.seedMilestone12', () => {
  it('calls the seed endpoint then reloads features', async () => {
    vi.mocked(featuresApi.seedMilestone12).mockResolvedValue({});
    vi.mocked(featuresApi.list).mockResolvedValue([makeFeature({ id: 'seeded' })]);
    vi.mocked(storiesApi.list).mockResolvedValue([]);

    await specStore.seedMilestone12();

    expect(featuresApi.seedMilestone12).toHaveBeenCalledOnce();
    expect(featuresApi.list).toHaveBeenCalledOnce();
    const state = get(specStore);
    expect(state.features.map(f => f.id)).toEqual(['seeded']);
    expect(state.seeding).toBe(false);
  });

  it('sets error, clears seeding flag, and rethrows on failure', async () => {
    vi.mocked(featuresApi.seedMilestone12).mockRejectedValue(new Error('seed failed'));

    await expect(specStore.seedMilestone12()).rejects.toThrow('seed failed');
    const state = get(specStore);
    expect(state.error).toBe('seed failed');
    expect(state.seeding).toBe(false);
    expect(featuresApi.list).not.toHaveBeenCalled();
  });
});

describe('expand state', () => {
  it('toggleFeature expands then collapses', () => {
    specStore.toggleFeature('f1');
    expect(get(specStore).expandedFeatureIds).toEqual(['f1']);
    specStore.toggleFeature('f1');
    expect(get(specStore).expandedFeatureIds).toEqual([]);
  });

  it('toggleStory lazy-loads criteria exactly once', async () => {
    vi.mocked(storiesApi.criteria).mockResolvedValue([makeCriterion()]);

    await specStore.toggleStory('s1'); // expand -> fetch
    await specStore.toggleStory('s1'); // collapse -> no fetch
    await specStore.toggleStory('s1'); // expand again -> cached, no fetch

    expect(storiesApi.criteria).toHaveBeenCalledTimes(1);
    const state = get(specStore);
    expect(state.criteriaByStory['s1']).toHaveLength(1);
    expect(state.expandedStoryIds).toEqual(['s1']);
  });

  it('toggleStory records error when the criteria fetch fails', async () => {
    vi.mocked(storiesApi.criteria).mockRejectedValue(new Error('no criteria'));

    await specStore.toggleStory('s1');

    const state = get(specStore);
    expect(state.error).toBe('no criteria');
    // Story stays expanded; criteria bucket stays absent so a retry refetches
    expect(state.expandedStoryIds).toEqual(['s1']);
    expect('s1' in state.criteriaByStory).toBe(false);
  });
});

describe('feature mutations', () => {
  it('createFeature appends and initializes an empty story bucket', async () => {
    const created = makeFeature({ id: 'new-f', title: 'Brand New' });
    vi.mocked(featuresApi.create).mockResolvedValue(created);

    const result = await specStore.createFeature({ title: 'Brand New' });

    expect(result).toEqual(created);
    const state = get(specStore);
    expect(state.features.map(f => f.id)).toEqual(['new-f']);
    expect(state.storiesByFeature['new-f']).toEqual([]);
  });

  it('updateFeature replaces in place without duplicating', async () => {
    vi.mocked(featuresApi.list).mockResolvedValue([makeFeature()]);
    vi.mocked(storiesApi.list).mockResolvedValue([]);
    await specStore.loadAll();

    vi.mocked(featuresApi.update).mockResolvedValue(
      makeFeature({ title: 'Renamed', status: 'active' })
    );
    await specStore.updateFeature('f1', { title: 'Renamed', status: 'active' });

    const state = get(specStore);
    expect(state.features).toHaveLength(1);
    expect(state.features[0].title).toBe('Renamed');
    expect(state.features[0].status).toBe('active');
  });

  it('deleteFeature removes the feature, its stories, and its expansion', async () => {
    vi.mocked(featuresApi.list).mockResolvedValue([makeFeature()]);
    vi.mocked(storiesApi.list).mockResolvedValue([makeStory()]);
    await specStore.loadAll();
    specStore.toggleFeature('f1');

    vi.mocked(featuresApi.delete).mockResolvedValue(undefined);
    await specStore.deleteFeature('f1');

    const state = get(specStore);
    expect(state.features).toEqual([]);
    expect('f1' in state.storiesByFeature).toBe(false);
    expect(state.expandedFeatureIds).toEqual([]);
  });

  it('createFeature sets error and rethrows on failure', async () => {
    vi.mocked(featuresApi.create).mockRejectedValue(new Error('duplicate title'));

    await expect(specStore.createFeature({ title: 'x' })).rejects.toThrow('duplicate title');
    expect(get(specStore).error).toBe('duplicate title');
  });
});

describe('story mutations', () => {
  it('createStory appends to its feature bucket', async () => {
    vi.mocked(featuresApi.list).mockResolvedValue([makeFeature()]);
    vi.mocked(storiesApi.list).mockResolvedValue([]);
    await specStore.loadAll();

    const story = makeStory({ id: 's-new', feature_id: 'f1' });
    vi.mocked(storiesApi.create).mockResolvedValue(story);

    await specStore.createStory({ feature_id: 'f1', title: 'Story One' });

    expect(get(specStore).storiesByFeature['f1'].map(s => s.id)).toEqual(['s-new']);
  });

  it('updateStory replaces the matching story only', async () => {
    vi.mocked(featuresApi.list).mockResolvedValue([makeFeature()]);
    vi.mocked(storiesApi.list).mockResolvedValue([
      makeStory({ id: 's1' }),
      makeStory({ id: 's2', title: 'Other' }),
    ]);
    await specStore.loadAll();

    vi.mocked(storiesApi.update).mockResolvedValue(
      makeStory({ id: 's1', title: 'Edited' })
    );
    await specStore.updateStory('s1', { title: 'Edited' });

    const stories = get(specStore).storiesByFeature['f1'];
    expect(stories).toHaveLength(2);
    expect(stories.find(s => s.id === 's1')?.title).toBe('Edited');
    expect(stories.find(s => s.id === 's2')?.title).toBe('Other');
  });

  it('deleteStory removes the story, its criteria, and its expansion', async () => {
    vi.mocked(featuresApi.list).mockResolvedValue([makeFeature()]);
    vi.mocked(storiesApi.list).mockResolvedValue([makeStory()]);
    await specStore.loadAll();
    vi.mocked(storiesApi.criteria).mockResolvedValue([makeCriterion()]);
    await specStore.toggleStory('s1');

    vi.mocked(storiesApi.delete).mockResolvedValue(undefined);
    await specStore.deleteStory('s1', 'f1');

    const state = get(specStore);
    expect(state.storiesByFeature['f1']).toEqual([]);
    expect('s1' in state.criteriaByStory).toBe(false);
    expect(state.expandedStoryIds).toEqual([]);
  });
});

describe('criterion mutations', () => {
  it('createCriterion appends to its story bucket', async () => {
    const criterion = makeCriterion({ id: 'c-new', user_story_id: 's1' });
    vi.mocked(criteriaApi.create).mockResolvedValue(criterion);

    await specStore.createCriterion({ user_story_id: 's1', text: 'It works' });

    expect(get(specStore).criteriaByStory['s1'].map(c => c.id)).toEqual(['c-new']);
  });

  it('updateCriterion replaces the matching criterion', async () => {
    vi.mocked(criteriaApi.create).mockResolvedValue(makeCriterion());
    await specStore.createCriterion({ user_story_id: 's1', text: 'It works' });

    vi.mocked(criteriaApi.update).mockResolvedValue(
      makeCriterion({ text: 'It really works', required: false })
    );
    await specStore.updateCriterion('c1', { text: 'It really works', required: false });

    const list = get(specStore).criteriaByStory['s1'];
    expect(list).toHaveLength(1);
    expect(list[0].text).toBe('It really works');
    expect(list[0].required).toBe(false);
  });

  it('deleteCriterion removes only the targeted criterion', async () => {
    vi.mocked(criteriaApi.create)
      .mockResolvedValueOnce(makeCriterion({ id: 'c1' }))
      .mockResolvedValueOnce(makeCriterion({ id: 'c2' }));
    await specStore.createCriterion({ user_story_id: 's1', text: 'a' });
    await specStore.createCriterion({ user_story_id: 's1', text: 'b' });

    vi.mocked(criteriaApi.delete).mockResolvedValue(undefined);
    await specStore.deleteCriterion('c1', 's1');

    expect(get(specStore).criteriaByStory['s1'].map(c => c.id)).toEqual(['c2']);
  });
});

describe('clear', () => {
  it('resets to the initial state', async () => {
    vi.mocked(featuresApi.list).mockResolvedValue([makeFeature()]);
    vi.mocked(storiesApi.list).mockResolvedValue([makeStory()]);
    await specStore.loadAll();
    specStore.toggleFeature('f1');

    specStore.clear();

    const state = get(specStore);
    expect(state.features).toEqual([]);
    expect(state.storiesByFeature).toEqual({});
    expect(state.expandedFeatureIds).toEqual([]);
    expect(state.error).toBeNull();
  });
});
