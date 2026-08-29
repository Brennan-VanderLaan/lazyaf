/**
 * Spec layer store (Phase 12.2.5).
 *
 * Holds the Feature -> UserStory -> AcceptanceCriterion tree for the Specs
 * page. Deliberately shallow: features load with their stories eagerly (so
 * story counts render without expanding), criteria lazy-load on first story
 * expand. Expansion state lives here so the tree survives component
 * re-renders.
 */
import { writable, derived } from 'svelte/store';
import type {
  Feature,
  FeatureCreate,
  FeatureUpdate,
  UserStory,
  UserStoryCreate,
  UserStoryUpdate,
  AcceptanceCriterion,
  AcceptanceCriterionCreate,
  AcceptanceCriterionUpdate,
} from '../api/types';
import {
  features as featuresApi,
  userStories as storiesApi,
  criteria as criteriaApi,
} from '../api/client';

export interface SpecState {
  features: Feature[];
  /** feature_id -> stories (eagerly loaded with features) */
  storiesByFeature: Record<string, UserStory[]>;
  /** user_story_id -> criteria (lazy: present only once a story was expanded) */
  criteriaByStory: Record<string, AcceptanceCriterion[]>;
  expandedFeatureIds: string[];
  expandedStoryIds: string[];
  loading: boolean;
  seeding: boolean;
  error: string | null;
}

function initialState(): SpecState {
  return {
    features: [],
    storiesByFeature: {},
    criteriaByStory: {},
    expandedFeatureIds: [],
    expandedStoryIds: [],
    loading: false,
    seeding: false,
    error: null,
  };
}

function toggle(ids: string[], id: string): string[] {
  return ids.includes(id) ? ids.filter(x => x !== id) : [...ids, id];
}

function createSpecStore() {
  const { subscribe, set, update } = writable<SpecState>(initialState());

  /**
   * Load all features plus their stories (counts render immediately).
   * Exactly 2 requests regardless of feature count: one for features, one
   * unfiltered story list grouped client-side (no 1+N per-feature fetches).
   * Criteria stay lazy per story expand.
   */
  async function loadAll() {
    update(s => ({ ...s, loading: true, error: null }));
    try {
      const [featureList, allStories] = await Promise.all([
        featuresApi.list(),
        storiesApi.list(),
      ]);
      const storiesByFeature: Record<string, UserStory[]> = {};
      for (const f of featureList) {
        storiesByFeature[f.id] = [];
      }
      for (const story of allStories) {
        // Stories whose feature is unknown (raced delete) are dropped rather
        // than crashing the grouping.
        storiesByFeature[story.feature_id]?.push(story);
      }
      update(s => ({
        ...s,
        features: featureList,
        storiesByFeature,
        loading: false,
      }));
    } catch (e) {
      update(s => ({
        ...s,
        loading: false,
        error: e instanceof Error ? e.message : 'Failed to load specs',
      }));
    }
  }

  return {
    subscribe,

    loadAll,

    /** Seed the three Milestone 12 north-star stories, then reload. */
    async seedMilestone12() {
      update(s => ({ ...s, seeding: true, error: null }));
      try {
        await featuresApi.seedMilestone12();
        await loadAll();
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to seed Milestone 12 specs',
        }));
        throw e;
      } finally {
        update(s => ({ ...s, seeding: false }));
      }
    },

    toggleFeature(featureId: string) {
      update(s => ({
        ...s,
        expandedFeatureIds: toggle(s.expandedFeatureIds, featureId),
      }));
    },

    /** Expand/collapse a story; criteria fetch lazily on first expand. */
    async toggleStory(storyId: string) {
      let needsLoad = false;
      update(s => {
        const expanding = !s.expandedStoryIds.includes(storyId);
        needsLoad = expanding && !(storyId in s.criteriaByStory);
        return { ...s, expandedStoryIds: toggle(s.expandedStoryIds, storyId) };
      });
      if (needsLoad) {
        try {
          const list = await storiesApi.criteria(storyId);
          update(s => ({
            ...s,
            criteriaByStory: { ...s.criteriaByStory, [storyId]: list },
          }));
        } catch (e) {
          update(s => ({
            ...s,
            error: e instanceof Error ? e.message : 'Failed to load criteria',
          }));
        }
      }
    },

    async createFeature(data: FeatureCreate): Promise<Feature> {
      update(s => ({ ...s, error: null }));
      try {
        const feature = await featuresApi.create(data);
        update(s => ({
          ...s,
          features: [...s.features, feature],
          storiesByFeature: { ...s.storiesByFeature, [feature.id]: [] },
        }));
        return feature;
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to create feature',
        }));
        throw e;
      }
    },

    async updateFeature(id: string, data: FeatureUpdate): Promise<Feature> {
      update(s => ({ ...s, error: null }));
      try {
        const feature = await featuresApi.update(id, data);
        update(s => ({
          ...s,
          features: s.features.map(f => (f.id === id ? feature : f)),
        }));
        return feature;
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to update feature',
        }));
        throw e;
      }
    },

    async deleteFeature(id: string) {
      update(s => ({ ...s, error: null }));
      try {
        await featuresApi.delete(id);
        update(s => {
          const storiesByFeature = { ...s.storiesByFeature };
          delete storiesByFeature[id];
          return {
            ...s,
            features: s.features.filter(f => f.id !== id),
            storiesByFeature,
            expandedFeatureIds: s.expandedFeatureIds.filter(x => x !== id),
          };
        });
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to delete feature',
        }));
        throw e;
      }
    },

    async createStory(data: UserStoryCreate): Promise<UserStory> {
      update(s => ({ ...s, error: null }));
      try {
        const story = await storiesApi.create(data);
        update(s => ({
          ...s,
          storiesByFeature: {
            ...s.storiesByFeature,
            [story.feature_id]: [...(s.storiesByFeature[story.feature_id] ?? []), story],
          },
        }));
        return story;
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to create story',
        }));
        throw e;
      }
    },

    async updateStory(id: string, data: UserStoryUpdate): Promise<UserStory> {
      update(s => ({ ...s, error: null }));
      try {
        const story = await storiesApi.update(id, data);
        update(s => ({
          ...s,
          storiesByFeature: {
            ...s.storiesByFeature,
            [story.feature_id]: (s.storiesByFeature[story.feature_id] ?? []).map(st =>
              st.id === id ? story : st
            ),
          },
        }));
        return story;
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to update story',
        }));
        throw e;
      }
    },

    async deleteStory(id: string, featureId: string) {
      update(s => ({ ...s, error: null }));
      try {
        await storiesApi.delete(id);
        update(s => {
          const criteriaByStory = { ...s.criteriaByStory };
          delete criteriaByStory[id];
          return {
            ...s,
            storiesByFeature: {
              ...s.storiesByFeature,
              [featureId]: (s.storiesByFeature[featureId] ?? []).filter(st => st.id !== id),
            },
            criteriaByStory,
            expandedStoryIds: s.expandedStoryIds.filter(x => x !== id),
          };
        });
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to delete story',
        }));
        throw e;
      }
    },

    async createCriterion(data: AcceptanceCriterionCreate): Promise<AcceptanceCriterion> {
      update(s => ({ ...s, error: null }));
      try {
        const criterion = await criteriaApi.create(data);
        update(s => ({
          ...s,
          criteriaByStory: {
            ...s.criteriaByStory,
            [criterion.user_story_id]: [
              ...(s.criteriaByStory[criterion.user_story_id] ?? []),
              criterion,
            ],
          },
        }));
        return criterion;
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to create criterion',
        }));
        throw e;
      }
    },

    async updateCriterion(id: string, data: AcceptanceCriterionUpdate): Promise<AcceptanceCriterion> {
      update(s => ({ ...s, error: null }));
      try {
        const criterion = await criteriaApi.update(id, data);
        update(s => ({
          ...s,
          criteriaByStory: {
            ...s.criteriaByStory,
            [criterion.user_story_id]: (s.criteriaByStory[criterion.user_story_id] ?? []).map(
              c => (c.id === id ? criterion : c)
            ),
          },
        }));
        return criterion;
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to update criterion',
        }));
        throw e;
      }
    },

    async deleteCriterion(id: string, storyId: string) {
      update(s => ({ ...s, error: null }));
      try {
        await criteriaApi.delete(id);
        update(s => ({
          ...s,
          criteriaByStory: {
            ...s.criteriaByStory,
            [storyId]: (s.criteriaByStory[storyId] ?? []).filter(c => c.id !== id),
          },
        }));
      } catch (e) {
        update(s => ({
          ...s,
          error: e instanceof Error ? e.message : 'Failed to delete criterion',
        }));
        throw e;
      }
    },

    clear() {
      set(initialState());
    },
  };
}

export const specStore = createSpecStore();

/** feature_id -> story count, for the collapsed feature list rows. */
export const featureStoryCounts = derived(specStore, ($spec) => {
  const counts: Record<string, number> = {};
  for (const f of $spec.features) {
    counts[f.id] = ($spec.storiesByFeature[f.id] ?? []).length;
  }
  return counts;
});
