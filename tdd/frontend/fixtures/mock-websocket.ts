/**
 * Mock WebSocket for testing WebSocket-dependent code.
 *
 * Usage:
 *   const mockWs = new MockWebSocket();
 *   // Replace global WebSocket
 *   vi.stubGlobal('WebSocket', vi.fn(() => mockWs));
 *
 *   // Simulate server messages
 *   mockWs.simulateOpen();
 *   mockWs.simulateMessage({ type: 'runner_status', payload: {...} });
 */

export type WebSocketState = 'CONNECTING' | 'OPEN' | 'CLOSING' | 'CLOSED';

export class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState: number = MockWebSocket.CONNECTING;
  url: string;

  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  private eventListeners: Map<string, Set<EventListener>> = new Map();

  // Track sent messages for assertions
  sentMessages: unknown[] = [];

  constructor(url: string) {
    this.url = url;
    // Auto-connect after a microtask (simulates async connection)
    queueMicrotask(() => {
      // Don't auto-open - let tests control this
    });
  }

  addEventListener(type: string, listener: EventListener): void {
    if (!this.eventListeners.has(type)) {
      this.eventListeners.set(type, new Set());
    }
    this.eventListeners.get(type)!.add(listener);
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.eventListeners.get(type)?.delete(listener);
  }

  send(data: string | ArrayBuffer | Blob): void {
    if (this.readyState !== MockWebSocket.OPEN) {
      throw new Error('WebSocket is not open');
    }
    try {
      this.sentMessages.push(JSON.parse(data as string));
    } catch {
      this.sentMessages.push(data);
    }
  }

  close(code?: number, reason?: string): void {
    this.readyState = MockWebSocket.CLOSING;
    queueMicrotask(() => {
      this.readyState = MockWebSocket.CLOSED;
      const event = new CloseEvent('close', { code, reason });
      this.onclose?.(event);
      this.dispatchEvent('close', event);
    });
  }

  // --- Test helpers ---

  /**
   * Simulate the WebSocket connection opening
   */
  simulateOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    const event = new Event('open');
    this.onopen?.(event);
    this.dispatchEvent('open', event);
  }

  /**
   * Simulate receiving a message from the server
   */
  simulateMessage(data: unknown): void {
    const event = new MessageEvent('message', {
      data: typeof data === 'string' ? data : JSON.stringify(data),
    });
    this.onmessage?.(event);
    this.dispatchEvent('message', event);
  }

  /**
   * Simulate a WebSocket error
   */
  simulateError(error?: Error): void {
    const event = new ErrorEvent('error', { error });
    this.onerror?.(event);
    this.dispatchEvent('error', event);
  }

  /**
   * Simulate the server closing the connection
   */
  simulateClose(code = 1000, reason = ''): void {
    this.readyState = MockWebSocket.CLOSED;
    const event = new CloseEvent('close', { code, reason });
    this.onclose?.(event);
    this.dispatchEvent('close', event);
  }

  /**
   * Get all messages sent by the client
   */
  getSentMessages(): unknown[] {
    return [...this.sentMessages];
  }

  /**
   * Clear sent messages (for test isolation)
   */
  clearSentMessages(): void {
    this.sentMessages = [];
  }

  private dispatchEvent(type: string, event: Event): void {
    const listeners = this.eventListeners.get(type);
    if (listeners) {
      listeners.forEach(listener => listener(event));
    }
  }
}

/**
 * Factory to create a MockWebSocket and track instances
 */
export function createMockWebSocketFactory() {
  const instances: MockWebSocket[] = [];

  const factory = (url: string): MockWebSocket => {
    const ws = new MockWebSocket(url);
    instances.push(ws);
    return ws;
  };

  return {
    factory,
    getInstances: () => [...instances],
    getLatest: () => instances[instances.length - 1],
    clear: () => {
      instances.length = 0;
    },
  };
}
