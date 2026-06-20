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

export async function sendChat(workspaceId: string, content: string): Promise<void> {
  await parse(
    await fetch(`${API_BASE}/api/workspaces/${workspaceId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content })
    })
  );
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
