import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileUp, PanelLeft } from "lucide-react";
import { useEffect } from "react";
import { getWorkspace, listWorkspaces, uploadWorkspace } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { PreviewPanel } from "./components/PreviewPanel";
import { WorkspaceSidebar } from "./components/WorkspaceSidebar";
import { useAppStore } from "./store";

export default function App() {
  const queryClient = useQueryClient();
  const { workspaceId, setWorkspaceId, sidebarOpen, toggleSidebar, setSidebarOpen } = useAppStore();

  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: listWorkspaces });
  const workspace = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => getWorkspace(workspaceId!),
    enabled: Boolean(workspaceId),
  });

  const upload = useMutation({
    mutationFn: uploadWorkspace,
    onSuccess: (created) => {
      setWorkspaceId(created.id);
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.setQueryData(["workspace", created.id], created);
    },
  });

  // Auto-select most recent workspace on first load.
  useEffect(() => {
    if (!workspaceId && workspaces.data?.[0]) {
      setWorkspaceId(workspaces.data[0].id);
    }
  }, [setWorkspaceId, workspaceId, workspaces.data]);

  return (
    <>
      <WorkspaceSidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      <main className="flex h-full min-h-0 bg-paper text-ink">
        {/* Left panel: chat + controls */}
        <section className="flex w-[35%] min-w-[360px] flex-col border-r-2 border-ink">
          <header className="flex h-16 items-center justify-between border-b-2 border-ink px-5">
            <div className="flex items-center gap-3">
              {/* Sidebar toggle */}
              <button
                id="open-sidebar-btn"
                className="flex h-9 w-9 items-center justify-center border-2 border-ink bg-paper hover:bg-accent transition-colors"
                title="Browse workspaces"
                onClick={toggleSidebar}
              >
                <PanelLeft size={17} />
              </button>
              <div>
                <h1 className="text-lg font-bold leading-tight">Document Agent</h1>
                <p className="text-xs font-medium">PPTX and DOCX workspace</p>
              </div>
            </div>
            <label
              className="inline-flex h-10 w-10 cursor-pointer items-center justify-center border-2 border-ink bg-accent"
              title="Upload document"
            >
              <FileUp size={19} />
              <input
                className="hidden"
                type="file"
                accept=".pptx,.docx"
                onChange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  if (file) upload.mutate(file);
                  event.currentTarget.value = "";
                }}
              />
            </label>
          </header>

          {workspace.data ? (
            <ChatPanel workspace={workspace.data} />
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center">
              <FileUp size={36} />
              <p className="max-w-xs text-sm font-semibold">
                Upload a PPTX or DOCX to start editing with the document agent,
                or click <PanelLeft size={12} className="inline" /> to open an existing workspace.
              </p>
              {upload.isPending && <p className="text-sm font-medium">Uploading and indexing...</p>}
              {upload.error && <p className="text-sm font-semibold">{upload.error.message}</p>}
            </div>
          )}
        </section>

        {/* Right panel: preview */}
        <section className="min-w-0 flex-1">
          <PreviewPanel workspace={workspace.data ?? null} />
        </section>
      </main>
    </>
  );
}
