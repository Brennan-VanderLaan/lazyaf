import { writable } from 'svelte/store';
import type { Card, Pipeline, PipelineRun, StepRun, Repo, RunnerStatusUpdate, StepLogUpdate, StepStatusUpdate } from '../api/types';
import { cardsStore } from './cards';
import { jobsStore, type JobStatusUpdate } from './jobs';
import { pipelinesStore, activeRunsStore } from './pipelines';
import { reposStore } from './repos';
import { debugStore, type DebugBreakpointEvent, type DebugStatusEvent, type DebugResumeEvent } from './debug';
import { handleRunnerStatusUpdate, clearRunners } from './runners';

export type WebSocketStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

interface WebSocketMessage {
  type: 'card_updated' | 'card_deleted' | 'job_status' | 'runner_status' | 'pipeline_updated' | 'pipeline_deleted' | 'pipeline_run_status' | 'step_run_status' | 'step_logs' | 'step_status' | 'repo_created' | 'repo_updated' | 'repo_deleted' | 'debug_breakpoint' | 'debug_status' | 'debug_resume';
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
      // Clear runner state since we won't receive updates while disconnected
      clearRunners();
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
        // Phase 12.6: Runner status is now pushed via WebSocket, no polling
        handleRunnerStatusUpdate(message.payload as RunnerStatusUpdate);
        break;
      case 'step_logs':
        // Phase 12.6: Step logs pushed from remote runners
        // TODO: Route to step log viewer if implemented
        console.debug('step_logs:', message.payload);
        break;
      case 'step_status':
        // Phase 12.6: Step status update from remote runner
        // This is more granular than step_run_status - for real-time updates
        console.debug('step_status:', message.payload);
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
      case 'debug_breakpoint':
        debugStore.handleBreakpoint(message.payload as DebugBreakpointEvent);
        break;
      case 'debug_status':
        debugStore.handleStatus(message.payload as DebugStatusEvent);
        break;
      case 'debug_resume':
        debugStore.handleResume(message.payload as DebugResumeEvent);
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
