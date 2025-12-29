import { writable, derived } from 'svelte/store';
import type { Card, CardCreate, CardUpdate, CardStatus } from '../api/types';
import { cards as cardsApi } from '../api/client';
import { selectedRepoId } from './repos';

function createCardsStore() {
  const { subscribe, set, update } = writable<Card[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },

    async load(repoId: string) {
      loading.set(true);
      error.set(null);
      try {
        const data = await cardsApi.list(repoId);
        set(data);
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to load cards');
      } finally {
        loading.set(false);
      }
    },

    async create(repoId: string, data: CardCreate) {
      error.set(null);
      try {
        const card = await cardsApi.create(repoId, data);
        update(cards => [...cards, card]);
        return card;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to create card');
        throw e;
      }
    },

    async update(id: string, data: CardUpdate) {
      error.set(null);
      try {
        const card = await cardsApi.update(id, data);
        update(cards => cards.map(c => c.id === id ? card : c));
        return card;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to update card');
        throw e;
      }
    },

    async delete(id: string) {
      error.set(null);
      try {
        await cardsApi.delete(id);
        update(cards => cards.filter(c => c.id !== id));
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to delete card');
        throw e;
      }
    },

    async start(id: string) {
      error.set(null);
      try {
        const card = await cardsApi.start(id);
        update(cards => cards.map(c => c.id === id ? card : c));
        return card;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to start card');
        throw e;
      }
    },

    async approve(id: string) {
      error.set(null);
      try {
        const card = await cardsApi.approve(id);
        update(cards => cards.map(c => c.id === id ? card : c));
        return card;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to approve card');
        throw e;
      }
    },

    async reject(id: string) {
      error.set(null);
      try {
        const card = await cardsApi.reject(id);
        update(cards => cards.map(c => c.id === id ? card : c));
        return card;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to reject card');
        throw e;
      }
    },

    async retry(id: string) {
      error.set(null);
      try {
        const card = await cardsApi.retry(id);
        update(cards => cards.map(c => c.id === id ? card : c));
        return card;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to retry card');
        throw e;
      }
    },

    updateLocal(card: Card) {
      update(cards => cards.map(c => c.id === card.id ? card : c));
    },

    clear() {
      set([]);
    },
  };
}

export const cardsStore = createCardsStore();

// Derived stores for each column
const STATUSES: CardStatus[] = ['todo', 'in_progress', 'in_review', 'done', 'failed'];

export const cardsByStatus = derived(cardsStore, ($cards) => {
  const grouped: Record<CardStatus, Card[]> = {
    todo: [],
    in_progress: [],
    in_review: [],
    done: [],
    failed: [],
  };

  for (const card of $cards) {
    grouped[card.status].push(card);
  }

  return grouped;
});
