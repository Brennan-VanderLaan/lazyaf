import { writable } from 'svelte/store';
import type { Card } from '../api/types';
import { cardsStore } from './cards';

export type WebSocketStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

interface WebSocketMessage {
  type: 'card_updated' | 'job_status' | 'runner_status';
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
      case 'job_status':
        // TODO: Update job store
        break;
      case 'runner_status':
        // TODO: Update runner store
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
