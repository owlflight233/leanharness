import { describe, expect, it } from "vitest";

import { parseNDJSONStream, streamChat } from "./chat";

describe("NDJSON stream parser", () => {
  it("parses events split across byte and Unicode boundaries", async () => {
    const encoder = new TextEncoder();
    const payload = encoder.encode(
      '{"type":"turn.started","sequence":0}\n' +
        '{"type":"content.delta","sequence":1,"content":"你好"}\n' +
        '{"type":"turn.completed","sequence":2,"finish_reason":"stop"}\n',
    );
    const unicodeStart = payload.indexOf(0xe4);
    const chunks = [
      payload.slice(0, 19),
      payload.slice(19, unicodeStart + 1),
      payload.slice(unicodeStart + 1),
    ];
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(chunk);
        controller.close();
      },
    });

    const events = [];
    for await (const event of parseNDJSONStream(stream)) events.push(event);

    expect(events.map((event) => event.type)).toEqual([
      "turn.started",
      "content.delta",
      "turn.completed",
    ]);
    expect(events[1]).toMatchObject({ content: "你好" });
  });

  it("rejects malformed and unknown events", async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('{"type":"unknown","sequence":0}\n'));
        controller.close();
      },
    });

    await expect(async () => {
      for await (const _event of parseNDJSONStream(stream)) {
        // Consume the stream.
      }
    }).rejects.toThrow("未知事件");
  });

  it("rejects a response that ends without a terminal event", async () => {
    const originalFetch = globalThis.fetch;
    const encoder = new TextEncoder();
    globalThis.fetch = async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(
              encoder.encode('{"type":"content.delta","sequence":0,"content":"partial"}\n'),
            );
            controller.close();
          },
        }),
        { status: 200 },
      );

    try {
      await expect(
        streamChat("hello", () => undefined, new AbortController().signal),
      ).rejects.toThrow("意外中断");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
