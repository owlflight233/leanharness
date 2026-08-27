export interface ModelStatus {
  configured: boolean;
  protocol: "openai-compatible";
  model: string | null;
}

export type ModelStatusLoader = (signal?: AbortSignal) => Promise<ModelStatus>;

export async function fetchModelStatus(signal?: AbortSignal): Promise<ModelStatus> {
  const response = await fetch("/api/v1/model/status", {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`Model status request failed with status ${response.status}`);
  }
  return (await response.json()) as ModelStatus;
}
