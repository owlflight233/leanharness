export interface WorkspaceSelection {
  workspace: string;
}

export interface WorkspaceClient {
  select(path: string): Promise<WorkspaceSelection>;
  create?: (path: string) => Promise<WorkspaceSelection>;
}

export const selectWorkspace = async (path: string): Promise<WorkspaceSelection> => {
  const response = await fetch("/api/v1/workspace", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { error?: { message?: string } } | null;
    throw new Error(body?.error?.message || `Workspace request failed with status ${response.status}`);
  }
  return (await response.json()) as WorkspaceSelection;
};

export const createWorkspace = async (path: string): Promise<WorkspaceSelection> => {
  const response = await fetch("/api/v1/workspace/create", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { error?: { message?: string } } | null;
    throw new Error(body?.error?.message || `Workspace creation failed with status ${response.status}`);
  }
  return (await response.json()) as WorkspaceSelection;
};
