import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, FileUp, FolderOpen, Trash2, X } from "lucide-react";
import { deleteWorkspace, listWorkspaces, uploadWorkspace } from "../api";
import { useAppStore } from "../store";
import type { Workspace } from "../types";

type Props = {
  open: boolean;
  onClose: () => void;
};

export function WorkspaceSidebar({ open, onClose }: Props) {
  const { workspaceId, setWorkspaceId } = useAppStore();
  const queryClient = useQueryClient();

  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: listWorkspaces });

  const upload = useMutation({
    mutationFn: uploadWorkspace,
    onSuccess: (created) => {
      setWorkspaceId(created.id);
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.setQueryData(["workspace", created.id], created);
      onClose();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteWorkspace,
    onSuccess: (_data, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.removeQueries({ queryKey: ["workspace", deletedId] });
      // If the deleted workspace was active, clear the selection.
      if (workspaceId === deletedId) {
        const remaining = workspaces.data?.filter((ws) => ws.id !== deletedId) ?? [];
        setWorkspaceId(remaining[0]?.id ?? null);
      }
    },
  });

  function handleSelect(id: string) {
    setWorkspaceId(id);
    onClose();
  }

  function handleDelete(e: React.MouseEvent, id: string, name: string) {
    e.stopPropagation(); // Don't trigger handleSelect.
    if (!window.confirm(`Delete "${name}" and all its versions? This cannot be undone.`)) return;
    deleteMutation.mutate(id);
  }

  return (
    <>
      {/* Backdrop */}
      <div
        className={`fixed inset-0 z-20 bg-black/40 transition-opacity duration-300 ${open ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none"}`}
        onClick={onClose}
      />

      {/* Drawer */}
      <aside
        className={`fixed left-0 top-0 z-30 flex h-full w-72 flex-col border-r-2 border-ink bg-paper shadow-2xl transition-transform duration-300 ${open ? "translate-x-0" : "-translate-x-full"}`}
      >
        {/* Header */}
        <div className="flex h-16 items-center justify-between border-b-2 border-ink px-5">
          <div className="flex items-center gap-2">
            <FolderOpen size={18} />
            <span className="font-bold">Workspaces</span>
          </div>
          <button
            id="sidebar-close-btn"
            className="flex h-8 w-8 items-center justify-center border-2 border-ink bg-paper hover:bg-accent transition-colors"
            onClick={onClose}
            title="Close sidebar"
          >
            <X size={15} />
          </button>
        </div>

        {/* Upload button */}
        <label
          className="mx-4 mt-4 flex cursor-pointer items-center justify-center gap-2 border-2 border-ink bg-accent py-2.5 text-sm font-bold hover:opacity-80 transition-opacity"
          title="Upload new document"
        >
          <FileUp size={16} />
          {upload.isPending ? "Uploading…" : "Upload New Document"}
          <input
            className="hidden"
            type="file"
            accept=".pptx,.docx"
            onChange={(e) => {
              const file = e.currentTarget.files?.[0];
              if (file) upload.mutate(file);
              e.currentTarget.value = "";
            }}
          />
        </label>

        {upload.error && (
          <p className="mx-4 mt-2 text-xs font-semibold text-red-600">{upload.error.message}</p>
        )}
        {deleteMutation.error && (
          <p className="mx-4 mt-2 text-xs font-semibold text-red-600">{deleteMutation.error.message}</p>
        )}

        {/* Workspace list */}
        <div className="mt-4 flex-1 overflow-y-auto px-2">
          {workspaces.isLoading && (
            <p className="px-3 py-2 text-xs font-semibold">Loading…</p>
          )}
          {workspaces.data?.length === 0 && (
            <p className="px-3 py-2 text-xs font-semibold">No documents yet. Upload one above.</p>
          )}
          {workspaces.data?.map((ws: Workspace) => (
            <div
              key={ws.id}
              className={`group flex items-start gap-3 border-2 border-transparent transition-colors hover:border-ink hover:bg-accent ${ws.id === workspaceId ? "border-ink bg-accent" : ""}`}
            >
              {/* Workspace info — clickable to select */}
              <button
                id={`workspace-btn-${ws.id}`}
                className="flex min-w-0 flex-1 items-start gap-3 px-3 py-3 text-left"
                onClick={() => handleSelect(ws.id)}
              >
                <FileText size={16} className="mt-0.5 shrink-0" />
                <div className="min-w-0">
                  <p className="truncate text-sm font-bold leading-tight">{ws.original_filename}</p>
                  <p className="mt-0.5 text-xs font-medium uppercase text-ink/60">
                    {ws.document_type} · v{ws.current_version}
                  </p>
                  <p className="mt-0.5 text-xs text-ink/50">
                    {new Date(ws.updated_at).toLocaleDateString(undefined, {
                      month: "short",
                      day: "numeric",
                      year: "numeric",
                    })}
                  </p>
                </div>
              </button>

              {/* Delete button — only visible on hover */}
              <button
                id={`workspace-delete-btn-${ws.id}`}
                className="mr-2 mt-3 flex h-7 w-7 shrink-0 items-center justify-center border-2 border-transparent text-ink/40 opacity-0 transition-opacity hover:border-ink hover:text-ink group-hover:opacity-100"
                title={`Delete ${ws.original_filename}`}
                onClick={(e) => handleDelete(e, ws.id, ws.original_filename)}
                disabled={deleteMutation.isPending}
              >
                <Trash2 size={13} />
              </button>
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}
