import { afterEach, describe, expect, it, vi } from "vitest";

import { CondorWebSocket } from "./websocket";

afterEach(() => vi.unstubAllGlobals());

describe("cookie-authenticated WebSockets", () => {
  it.each([
    ["http://127.0.0.1:5173", "ws://127.0.0.1:5173/api/v1/ws"],
    ["https://griffin.example", "wss://griffin.example/api/v1/ws"],
  ])("keeps the cookie host for %s without sending a credential subprotocol", (origin, url) => {
    vi.stubGlobal("window", { location: new URL(origin) });
    const Socket = vi.fn(class { close = vi.fn(); });
    vi.stubGlobal("WebSocket", Socket);
    const socket = new CondorWebSocket();
    socket.connect();
    expect(Socket).toHaveBeenCalledExactlyOnceWith(url);
    socket.disconnect();
  });
});
