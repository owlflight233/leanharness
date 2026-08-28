import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PlanCard } from "./PlanCard";
import type { Plan } from "../api/plans";

const plan: Plan = {
  id: "plan-1",
  session_id: "session-1",
  title: "Demo plan",
  task: "Inspect the project",
  state: "AWAITING_CONFIRMATION",
  version: 1,
  source_markdown: "# Demo plan\n\n1. **Inspect** - Read the project",
  run_id: null,
  steps: [{ id: "step-1", sequence: 1, title: "Inspect", instruction: "Read the project", enabled: true, state: "PENDING" }],
};

describe("PlanCard", () => {
  it("renders markdown in the conversation and exposes confirmation actions", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<PlanCard plan={plan} onConfirm={onConfirm} onResume={vi.fn()} onReject={vi.fn()} onEdit={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Demo plan", level: 3 })).toBeInTheDocument();
    expect(screen.getByText("Read the project")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /确认执行/ }));
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});
