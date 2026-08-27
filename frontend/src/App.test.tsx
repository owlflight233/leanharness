import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import App from "./App";
import type { HealthLoader, HealthResponse } from "./api/health";

const healthy: HealthResponse = {
  status: "ok",
  name: "LeanHarness",
  version: "0.1.0.dev0",
  workspace: "C:\\projects\\demo",
  capabilities: [],
};

const successfulHealth: HealthLoader = async () => healthy;

describe("application shell", () => {
  it("renders a loading state without claiming agent capabilities", () => {
    const pendingHealth: HealthLoader = () => new Promise(() => undefined);
    render(<App healthLoader={pendingHealth} />);

    expect(screen.getByText("LeanHarness")).toBeInTheDocument();
    expect(screen.getByText("正在连接本地服务")).toBeInTheDocument();
    expect(screen.getByText("服务连接中")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送任务" })).toBeDisabled();
  });

  it("renders backend health and workspace", async () => {
    render(<App healthLoader={successfulHealth} />);

    expect(await screen.findByText("工作区已连接")).toBeInTheDocument();
    expect(screen.getByText("服务在线")).toBeInTheDocument();
    expect(screen.getByText("工作区：C:\\projects\\demo")).toBeInTheDocument();
  });

  it("renders a failed connection", async () => {
    const failedHealth: HealthLoader = async () => {
      throw new Error("offline");
    };
    render(<App healthLoader={failedHealth} />);

    await waitFor(() => expect(screen.getByText("本地服务不可用")).toBeInTheDocument());
    expect(screen.getByText("服务离线")).toBeInTheDocument();
  });

  it("switches inspector projections", async () => {
    const user = userEvent.setup();
    render(<App healthLoader={successfulHealth} />);

    await user.click(screen.getByRole("tab", { name: "轨迹" }));

    expect(screen.getByText("没有运行轨迹")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "轨迹" })).toHaveAttribute("aria-selected", "true");
  });

  it("opens and closes the project drawer", async () => {
    const user = userEvent.setup();
    render(<App healthLoader={successfulHealth} />);

    await user.click(screen.getByRole("button", { name: "打开项目导航" }));
    expect(screen.getByLabelText("项目导航")).toHaveClass("is-open");

    await user.click(screen.getByRole("button", { name: "关闭项目导航" }));
    expect(screen.getByLabelText("项目导航")).not.toHaveClass("is-open");
  });
});
