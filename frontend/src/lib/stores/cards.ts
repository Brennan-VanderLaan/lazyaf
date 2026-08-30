import { writable, derived, get } from 'svelte/store';
import type { Card, CardCreate, CardUpdate, CardStatus, ApproveResponse, RebaseResponse } from '../api/types';
import { cards as cardsApi } from '../api/client';
import { selectedRepoId } from './repos';

function createCardsStore() {
  const { subscribe, set, update } = writable<Card[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);

  /**
   * Insert-or-replace by id. THE only way a card enters this list.
   *
   * `updateLocal` used to REFUSE to add an unknown card ("adding is handled
   * by create() to avoid race conditions with WebSocket"). That does dodge the
   * duplicate row, but it also means a card created anywhere other than this
   * tab - the CLI, a second browser, a teammate during a demo - never appears
   * until someone reloads: the board silently shows TO DO 0 while a card sits
   * in it. Keying on the id fixes both halves at once, which is what
   * stores/repos.ts does for the same race.
   *
   * The repo guard is the reason this is not a straight copy of that one: this
   * store holds ONE repo's board, so a frame for a card in another repository
   * must not be adopted into it.
   */
  function upsert(card: Card) {
    update(cards => {
      const index = cards.findIndex(c => c.id === card.id);
      if (index >= 0) {
        const next = [...cards];
        next[index] = card;
        return next;
      }
      if (card.repo_id !== get(selectedRepoId)) return cards;
      return [...cards, card];
    });
  }

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
        upsert(card);
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

    async approve(id: string, targetBranch?: string): Promise<ApproveResponse> {
      error.set(null);
      try {
        const response = await cardsApi.approve(id, targetBranch);
        update(cards => cards.map(c => c.id === id ? response.card : c));
        return response;
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

    async rebase(id: string, ontoBranch?: string): Promise<RebaseResponse> {
      error.set(null);
      try {
        const response = await cardsApi.rebase(id, ontoBranch);
        update(cards => cards.map(c => c.id === id ? response.card : c));
        return response;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to rebase card');
        throw e;
      }
    },

    async resolveConflicts(id: string, targetBranch: string | undefined, resolutions: Array<{ path: string; content: string }>): Promise<ApproveResponse> {
      error.set(null);
      try {
        const response = await cardsApi.resolveConflicts(id, targetBranch, resolutions);
        update(cards => cards.map(c => c.id === id ? response.card : c));
        return response;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to resolve conflicts');
        throw e;
      }
    },

    async resolveRebaseConflicts(id: string, ontoBranch: string | undefined, resolutions: Array<{ path: string; content: string }>): Promise<RebaseResponse> {
      error.set(null);
      try {
        const response = await cardsApi.resolveRebaseConflicts(id, ontoBranch, resolutions);
        update(cards => cards.map(c => c.id === id ? response.card : c));
        return response;
      } catch (e) {
        error.set(e instanceof Error ? e.message : 'Failed to resolve rebase conflicts');
        throw e;
      }
    },

    updateLocal(card: Card) {
      upsert(card);
    },

    deleteLocal(id: string) {
      update(cards => cards.filter(c => c.id !== id));
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
