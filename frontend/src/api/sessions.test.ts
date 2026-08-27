import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSession,
  deleteSession,
  fetchSession,
  fetchSessions,
  updateSession,
} from "./sessions";

const summary = {
  id: "session-1",
  project_id: "project-1",
  title: "新会话",
  permission_mode: "inspect" as const,
  created_at: "2026-08-27T10:00:00+00:00",
  updated_at: "2026-08-27T10:00:00+00:00",
  last_run_state: null,
};

afterEach(() => vi.unstubAllGlobals());

describe("session API", () => {
  it("loads session lists and details", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({ sessions: [summary] }))
      .mockResolvedValueOnce(Response.json({ session: summary, messages: [], runs: [] }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchSessions()).toEqual([summary]);
    expect(await fetchSession("session/1")).toEqual({ session: summary, messages: [], runs: [] });
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/sessions/session%2F1");
  });

  it("creates, updates, and deletes one session with JSON contracts", async () => {
    const updated = { ...summary, title: "已重命名" };
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(summary))
      .mockResolvedValueOnce(Response.json(updated))
      .mockResolvedValueOnce(Response.json({ deleted: true, session_id: summary.id }));
    vi.stubGlobal("fetch", fetchMock);

    await createSession("inspect");
    await updateSession(summary.id, { title: "已重命名" });
    await deleteSession(summary.id);

    expect(JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body))).toEqual({
      permission_mode: "inspect",
    });
    expect((fetchMock.mock.calls[1]?.[1] as RequestInit).method).toBe("PATCH");
    expect((fetchMock.mock.calls[2]?.[1] as RequestInit).method).toBe("DELETE");
  });

  it("surfaces structured API errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        Response.json(
          { error: { code: "SESSION_NOT_FOUND", message: "会话不存在" } },
          { status: 404 },
        ),
      ),
    );

    await expect(fetchSession("missing")).rejects.toThrow("会话不存在");
  });
});
