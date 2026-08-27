export interface HealthResponse {
  status: "ok";
  name: "LeanHarness";
  version: string;
  workspace: string;
  capabilities: string[];
}

export type HealthLoader = (signal?: AbortSignal) => Promise<HealthResponse>;

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch("/api/v1/health", {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`Health request failed with status ${response.status}`);
  }
  return (await response.json()) as HealthResponse;
}
