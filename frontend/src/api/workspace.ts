export interface WorkspaceSelection {
  workspace: string;
}

export interface ProjectSummary {
  id: string;
  root_path: string;
  permission_mode: "inspect" | "approve" | "unrestricted";
  created_at: string;
  updated_at: string;
}

export interface ProjectList {
  current_workspace: string;
  projects: ProjectSummary[];
}

export interface WorkspaceClient {
  select(path: string): Promise<WorkspaceSelection>;
  create?: (path: string) => Promise<WorkspaceSelection>;
  list?: (signal?: AbortSignal) => Promise<ProjectList>;
}

export const listProjects = async (signal?: AbortSignal): Promise<ProjectList> => {
  const response = await fetch("/api/v1/projects", { headers: { Accept: "application/json" }, signal });
  if (!response.ok) throw new Error(`项目加载失败 (${response.status})`);
  return (await response.json()) as ProjectList;
};

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
