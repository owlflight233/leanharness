export interface PluginToolSummary {
  name: string;
  description: string;
  mutation: boolean;
}

export interface PluginSummary {
  id: string;
  name: string;
  version: string;
  description: string;
  protocol_version: string;
  enabled: boolean;
  tools: PluginToolSummary[];
  installed_at: string;
  updated_at: string;
}

export async function fetchPlugins(signal?: AbortSignal): Promise<PluginSummary[]> {
  const response = await fetch("/api/v1/plugins", {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) throw new Error(`插件加载失败 (${response.status})`);
  return ((await response.json()) as { plugins: PluginSummary[] }).plugins;
}
