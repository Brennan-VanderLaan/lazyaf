import { writable, derived, get } from 'svelte/store';
import { models as modelsApi, type ModelInfo } from '../api/client';

interface ModelsState {
  anthropic: ModelInfo[];
  google: ModelInfo[];
  loading: boolean;
  error: string | null;
  lastFetched: Date | null;
}

const initialState: ModelsState = {
  anthropic: [],
  google: [],
  loading: false,
  error: null,
  lastFetched: null,
};

// Fallback models in case API fails
const FALLBACK_ANTHROPIC: ModelInfo[] = [
  { id: 'claude-sonnet-4-5-20250929', name: 'Claude Sonnet 4.5', provider: 'anthropic', description: 'Fast, 1M context' },
  { id: 'claude-sonnet-4-20250514', name: 'Claude Sonnet 4', provider: 'anthropic', description: 'Stable, fast' },
];

const FALLBACK_GOOGLE: ModelInfo[] = [
  { id: 'gemini-2.5-flash', name: 'Gemini 2.5 Flash', provider: 'google', description: 'Fast and efficient' },
  { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', provider: 'google', description: 'Most capable' },
];

function createModelsStore() {
  const { subscribe, set, update } = writable<ModelsState>(initialState);

  const store = { subscribe, set, update };

  return {
    subscribe,

    async load(forceRefresh: boolean = false) {
      // Check if we need to refresh (cache for 1 hour on frontend too)
      const state = get(store);

      const cacheValid = state.lastFetched &&
        (Date.now() - state.lastFetched.getTime()) < 60 * 60 * 1000;

      if (!forceRefresh && cacheValid && state.anthropic.length > 0) {
        return;
      }

      update((s) => ({ ...s, loading: true, error: null }));

      try {
        const response = await modelsApi.list(forceRefresh);
        update((s) => ({
          ...s,
          anthropic: response.anthropic.length > 0 ? response.anthropic : FALLBACK_ANTHROPIC,
          google: response.google.length > 0 ? response.google : FALLBACK_GOOGLE,
          loading: false,
          error: null,
          lastFetched: new Date(),
        }));
      } catch (e) {
        console.error('Failed to load models:', e);
        update((s) => ({
          ...s,
          anthropic: FALLBACK_ANTHROPIC,
          google: FALLBACK_GOOGLE,
          loading: false,
          error: e instanceof Error ? e.message : 'Failed to load models',
          lastFetched: new Date(),
        }));
      }
    },

    reset() {
      set(initialState);
    },
  };
}

export const modelsStore = createModelsStore();

// Derived stores for easy access
export const claudeModels = derived(modelsStore, ($state) => $state.anthropic);
export const geminiModels = derived(modelsStore, ($state) => $state.google);
export const modelsLoading = derived(modelsStore, ($state) => $state.loading);
