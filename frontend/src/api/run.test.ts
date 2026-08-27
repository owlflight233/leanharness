import { describe, expect, it } from "vitest";

import { parseRunStream, streamRun } from "./run";

describe("agent run stream", () => {
  it("parses events across byte and Unicode boundaries", async () => {
    const encoder = new TextEncoder();
    const payload = encoder.encode(
      '{"type":"run.started","sequence":0,"run_id":"r1"}\n' +
        '{"type":"assistant.progress","sequence":1,"run_id":"r1","summary":"检查代码"}\n' +
        '{"type":"run.completed","sequence":2,"run_id":"r1","answer":"完成"}\n',
    );
    const unicodeStart = payload.indexOf(0xe6);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(payload.slice(0, unicodeStart + 1));
        controller.enqueue(payload.slice(unicodeStart + 1));
        controller.close();
      },
    });

    const events = [];
    for await (const event of parseRunStream(stream)) events.push(event);

    expect(events.map((event) => event.type)).toEqual([
      "run.started",
      "assistant.progress",
      "run.completed",
    ]);
  });

  it("rejects streams without a terminal event", async () => {
    const originalFetch = globalThis.fetch;
    const encoder = new TextEncoder();
    globalThis.fetch = async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(
              encoder.encode('{"type":"run.started","sequence":0,"run_id":"r1"}\n'),
            );
            controller.close();
          },
        }),
        { status: 200 },
      );
    try {
      await expect(
        streamRun("inspect", () => undefined, new AbortController().signal),
      ).rejects.toThrow("意外中断");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
