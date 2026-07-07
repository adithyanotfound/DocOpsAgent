import type { Workspace } from "./types";

export const API_BASE = "";

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? "Request failed");
  }
  return response.json() as Promise<T>;
}

export async function listWorkspaces(): Promise<Workspace[]> {
  return parse<Workspace[]>(await fetch(`${API_BASE}/api/workspaces`));
}

export async function getWorkspace(id: string): Promise<Workspace> {
  return parse<Workspace>(await fetch(`${API_BASE}/api/workspaces/${id}`));
}

export async function uploadWorkspace(file: File): Promise<Workspace> {
  const form = new FormData();
  form.append("file", file);
  return parse<Workspace>(
    await fetch(`${API_BASE}/api/workspaces`, {
      method: "POST",
      body: form
    })
  );
}

export async function deleteWorkspace(id: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/workspaces/${id}`, { method: "DELETE" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? "Delete failed");
  }
}

export async function startChat(workspaceId: string, content: string, image?: File): Promise<{ run_id: string }> {
  const form = new FormData();
  form.append("workspace_id", workspaceId);
  form.append("content", content);
  if (image) {
    form.append("image", image);
  }
  return parse<{ run_id: string }>(
    await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      body: form,
      // Do NOT set Content-Type; browser will set multipart boundary automatically
    })
  );
}

export type PollEvent = 
  | { type: "thought"; content: string; iteration: number }
  | { type: "version_created"; version_number: number; pdf_url: string; document_url: string };

export type PollResponse = {
  status: "running" | "completed" | "error" | "not_found";
  events: PollEvent[];
  workspace: Workspace | null;
  error: string | null;
};

export async function pollRun(runId: string): Promise<PollResponse> {
  return parse<PollResponse>(await fetch(`${API_BASE}/api/polling/${runId}`));
}

export async function rollback(workspaceId: string, version: number): Promise<Workspace> {
  return parse<Workspace>(
    await fetch(`${API_BASE}/api/workspaces/${workspaceId}/rollback/${version}`, {
      method: "POST"
    })
  );
}

export function fileUrl(path: string): string {
  return path;
}
