import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import App from "./App";

describe("application shell", () => {
  it("renders the foundation workspace without claiming agent capabilities", () => {
    render(<App />);

    expect(screen.getByText("LeanHarness")).toBeInTheDocument();
    expect(screen.getByText("准备连接本地服务")).toBeInTheDocument();
    expect(screen.getByText("服务未连接")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送任务" })).toBeDisabled();
  });

  it("switches inspector projections", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("tab", { name: "轨迹" }));

    expect(screen.getByText("没有运行轨迹")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "轨迹" })).toHaveAttribute("aria-selected", "true");
  });

  it("opens and closes the project drawer", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: "打开项目导航" }));
    expect(screen.getByLabelText("项目导航")).toHaveClass("is-open");

    await user.click(screen.getByRole("button", { name: "关闭项目导航" }));
    expect(screen.getByLabelText("项目导航")).not.toHaveClass("is-open");
  });
});
