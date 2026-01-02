import { writable } from 'svelte/store';
import type { Card, Pipeline, PipelineRun, StepRun, Repo } from '../api/types';
import { cardsStore } from './cards';
import { jobsStore, type JobStatusUpdate } from './jobs';
import { pipelinesStore, activeRunsStore } from './pipelines';
import { reposStore } from './repos';

export type WebSocketStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

interface WebSocketMessage {
  type: 'card_updated' | 'card_deleted' | 'job_status' | 'runner_status' | 'pipeline_updated' | 'pipeline_deleted' | 'pipeline_run_status' | 'step_run_status' | 'repo_created' | 'repo_updated' | 'repo_deleted';
  payload: unknown;
}

function createWebSocketStore() {
  const status = writable<WebSocketStatus>('disconnected');
  let ws: WebSocket | null = null;
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    if (ws?.readyState === WebSocket.OPEN) return;

    status.set('connecting');

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      status.set('connected');
      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }
    };

    ws.onmessage = (event) => {
      try {
        const message: WebSocketMessage = JSON.parse(event.data);
        handleMessage(message);
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e);
      }
    };

    ws.onerror = () => {
      status.set('error');
    };

    ws.onclose = () => {
      status.set('disconnected');
      ws = null;
      // Reconnect after 3 seconds
      reconnectTimeout = setTimeout(connect, 3000);
    };
  }

  function handleMessage(message: WebSocketMessage) {
    switch (message.type) {
      case 'card_updated':
        cardsStore.updateLocal(message.payload as Card);
        break;
      case 'card_deleted':
        cardsStore.deleteLocal((message.payload as { id: string }).id);
        break;
      case 'job_status':
        jobsStore.updateFromWebSocket(message.payload as JobStatusUpdate);
        break;
      case 'runner_status':
        // Runner status is handled by polling in runnersStore
        break;
      case 'pipeline_updated':
        pipelinesStore.updateLocal(message.payload as Pipeline);
        break;
      case 'pipeline_deleted':
        pipelinesStore.deleteLocal((message.payload as { id: string }).id);
        break;
      case 'pipeline_run_status':
        activeRunsStore.updateRun(message.payload as PipelineRun);
        break;
      case 'step_run_status':
        // Step status updates are included in pipeline_run_status
        // This is for more granular updates if needed
        break;
      case 'repo_created':
      case 'repo_updated':
        reposStore.updateLocal(message.payload as Repo);
        break;
      case 'repo_deleted':
        reposStore.deleteLocal((message.payload as { id: string }).id);
        break;
    }
  }

  function disconnect() {
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }
    if (ws) {
      ws.close();
      ws = null;
    }
    status.set('disconnected');
  }

  return {
    status: { subscribe: status.subscribe },
    connect,
    disconnect,
  };
}

export const websocketStore = createWebSocketStore();
