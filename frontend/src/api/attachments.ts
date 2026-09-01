export interface Attachment {
  id: string;
  session_id?: string;
  message_id?: string | null;
  filename: string;
  media_type: string;
  kind: "image" | "text";
  byte_size: number;
  sha256: string;
  created_at: string;
}

export async function uploadAttachment(
  sessionId: string,
  file: File,
  signal?: AbortSignal,
): Promise<Attachment> {
  const body = new FormData();
  body.append("file", file, file.name);
  const response = await fetch(
    `/api/v1/attachments?session_id=${encodeURIComponent(sessionId)}`,
    { method: "POST", body, signal },
  );
  if (!response.ok) throw new Error(await readAttachmentError(response));
  return (await response.json()) as Attachment;
}

export async function deleteAttachment(id: string): Promise<void> {
  const response = await fetch(`/api/v1/attachments/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(await readAttachmentError(response));
}

async function readAttachmentError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { error?: { message?: string } };
    return payload.error?.message || `附件请求失败 (${response.status})`;
  } catch {
    return `附件请求失败 (${response.status})`;
  }
}
