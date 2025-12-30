import { writable } from 'svelte/store';
import { agentFiles as agentFilesApi } from '../api/client';
import type { AgentFile } from '../api/types';

function createAgentFilesStore() {
  const { subscribe, set, update } = writable<AgentFile[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);

  async function load() {
    loading.set(true);
    error.set(null);
    try {
      const data = await agentFilesApi.list();
      set(data);
    } catch (err) {
      error.set(err instanceof Error ? err.message : 'Failed to load agent files');
      console.error('Error loading agent files:', err);
    } finally {
      loading.set(false);
    }
  }

  async function create(name: string, content: string, description?: string | null) {
    loading.set(true);
    error.set(null);
    try {
      const newAgentFile = await agentFilesApi.create({ name, content, description });
      update(files => [...files, newAgentFile]);
      return newAgentFile;
    } catch (err) {
      error.set(err instanceof Error ? err.message : 'Failed to create agent file');
      console.error('Error creating agent file:', err);
      throw err;
    } finally {
      loading.set(false);
    }
  }

  async function updateAgentFile(id: string, name?: string, content?: string, description?: string | null) {
    loading.set(true);
    error.set(null);
    try {
      const updatedAgentFile = await agentFilesApi.update(id, { name, content, description });
      update(files => files.map(f => f.id === id ? updatedAgentFile : f));
      return updatedAgentFile;
    } catch (err) {
      error.set(err instanceof Error ? err.message : 'Failed to update agent file');
      console.error('Error updating agent file:', err);
      throw err;
    } finally {
      loading.set(false);
    }
  }

  async function deleteAgentFile(id: string) {
    loading.set(true);
    error.set(null);
    try {
      await agentFilesApi.delete(id);
      update(files => files.filter(f => f.id !== id));
    } catch (err) {
      error.set(err instanceof Error ? err.message : 'Failed to delete agent file');
      console.error('Error deleting agent file:', err);
      throw err;
    } finally {
      loading.set(false);
    }
  }

  return {
    subscribe,
    loading,
    error,
    load,
    create,
    update: updateAgentFile,
    delete: deleteAgentFile,
  };
}

export const agentFilesStore = createAgentFilesStore();
