import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, FileText, RotateCcw, Send } from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";
import { rollback, sendChat } from "../api";
import { useAppStore } from "../store";
import type { AgentContent, SocketEvent, Workspace } from "../types";

type Props = {
  workspace: Workspace;
};

// ---------------------------------------------------------------------------
// Root component
// ---------------------------------------------------------------------------

export function ChatPanel({ workspace }: Props) {
  const {
    pinnedVersion,
    setPinnedVersion,
    isAgentRunning,
    liveUserMessage,
    liveThoughts,
    liveVersionTile,
    startAgentRun,
    pushThought,
    setLiveVersionTile,
    endAgentRun,
  } = useAppStore();

  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // ---- WebSocket ------------------------------------------------------
  useEffect(() => {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(
      `${protocol}://${window.location.host}/api/workspaces/${workspace.id}/ws`
    );

    socket.onmessage = async (msg) => {
      const event = JSON.parse(msg.data) as SocketEvent;

      if (event.type === "thought") {
        pushThought(event.content);
      }

      if (event.type === "version_created") {
        setLiveVersionTile({
          version_number: event.version_number,
          pdf_url: event.pdf_url,
          document_url: event.document_url,
        });
      }

      if (event.type === "completed" || event.type === "error") {
        // Await the refetch so the new message is in the cache BEFORE
        // the loading bubble disappears.
        await queryClient.refetchQueries({ queryKey: ["workspace", workspace.id] });
        await queryClient.refetchQueries({ queryKey: ["workspaces"] });
        endAgentRun();
      }
    };

    return () => socket.close();
  }, [workspace.id, pushThought, setLiveVersionTile, endAgentRun, queryClient]);

  // ---- Auto-scroll ---------------------------------------------------
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [workspace.messages, liveThoughts, liveUserMessage, isAgentRunning]);

  // ---- Chat mutation -------------------------------------------------
  const chatMutation = useMutation({
    mutationFn: (content: string) => sendChat(workspace.id, content),
    onMutate: (content) => startAgentRun(content),
    onSuccess: async () => {
      // Fallback: The POST request waits for the agent to finish. 
      // If we missed the WebSocket "completed" event, we catch up here.
      await queryClient.refetchQueries({ queryKey: ["workspace", workspace.id] });
      await queryClient.refetchQueries({ queryKey: ["workspaces"] });
    },
    onSettled: () => {
      // Safety net: always stop the spinner when the request finishes,
      // regardless of success, failure, or WebSocket state.
      endAgentRun();
    },
  });

  const rollbackMutation = useMutation({
    mutationFn: (version: number) => rollback(workspace.id, version),
    onSuccess: (updated) => {
      queryClient.setQueryData(["workspace", workspace.id], updated);
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const content = draft.trim();
    if (!content || isAgentRunning) return;
    setDraft("");
    chatMutation.mutate(content);
  }

  // ---- Render --------------------------------------------------------
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Version pills + refresh */}
      <div className="border-b-2 border-ink px-5 py-3">
        <p className="truncate text-xs font-bold text-ink/60 mb-2">{workspace.original_filename}</p>
        <div className="flex flex-wrap gap-1.5">
          {workspace.versions.map((v) => (
            <button
              key={v.id}
              className={`h-7 border-2 border-ink px-2.5 text-xs font-bold transition-colors hover:bg-accent ${
                v.version_number === workspace.current_version ? "bg-accent" : "bg-paper"
              }`}
              onClick={() => rollbackMutation.mutate(v.version_number)}
              title={`Restore version ${v.version_number}`}
            >
              v{v.version_number}
            </button>
          ))}
          <button
            className="flex h-7 w-7 items-center justify-center border-2 border-ink bg-paper hover:bg-accent"
            title="Refresh"
            onClick={() => queryClient.invalidateQueries({ queryKey: ["workspace", workspace.id] })}
          >
            <RotateCcw size={12} className="mx-auto" />
          </button>
        </div>
      </div>

      {/* Message list */}
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="flex flex-col gap-4">
          {workspace.messages.map((message) => {
            if (message.role === "user") {
              return <UserBubble key={message.id} content={message.content} />;
            }
            return (
              <AgentBubble
                key={message.id}
                content={message.content_parsed}
                rawContent={message.content}
                onVersionClick={(vn) => setPinnedVersion(vn)}
              />
            );
          })}

          {/* Optimistically rendered user message */}
          {isAgentRunning && liveUserMessage && (
            <UserBubble content={liveUserMessage} />
          )}

          {/* Live in-progress bubble */}
          {isAgentRunning && (
            <LiveAgentBubble
              thoughts={liveThoughts}
              versionTile={liveVersionTile}
              onVersionClick={(vn) => setPinnedVersion(vn)}
            />
          )}
        </div>
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <form onSubmit={onSubmit} className="flex gap-2 border-t-2 border-ink p-4">
        <textarea
          className="min-h-20 flex-1 resize-none border-2 border-ink bg-paper p-3 text-sm font-medium outline-none focus:bg-accent/30 transition-colors"
          value={draft}
          placeholder="Ask the agent to edit the document…"
          disabled={isAgentRunning}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSubmit(e as unknown as FormEvent);
            }
          }}
        />
        <button
          className="flex w-12 items-center justify-center border-2 border-ink bg-accent disabled:opacity-40"
          title="Send"
          disabled={isAgentRunning || !draft.trim()}
        >
          <Send size={18} />
        </button>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// User message bubble
// ---------------------------------------------------------------------------

function UserBubble({ content }: { content: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-ink px-4 py-3 text-paper text-sm font-medium shadow-sm">
        {content}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Historical agent message bubble
// ---------------------------------------------------------------------------

function AgentBubble({
  content,
  rawContent,
  onVersionClick,
}: {
  content: AgentContent | null;
  rawContent: string;
  onVersionClick: (v: number) => void;
}) {
  if (!content) {
    // Plain text (e.g. "Document uploaded and indexed.")
    return (
      <div className="flex justify-start">
        <div className="max-w-[85%] text-sm text-ink/70 font-medium py-1">{rawContent}</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {content.thoughts.length > 0 && (
        <ThinkingBlock thoughts={content.thoughts} />
      )}
      {content.version_number != null && (
        <VersionTile
          versionNumber={content.version_number}
          label={content.version_label ?? ""}
          onClick={() => onVersionClick(content.version_number!)}
        />
      )}
      <div className="flex justify-start">
        <p className="max-w-[85%] text-sm text-ink leading-relaxed">{content.text}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live in-progress bubble while agent is running
// ---------------------------------------------------------------------------

function LiveAgentBubble({
  thoughts,
  versionTile,
  onVersionClick,
}: {
  thoughts: string[];
  versionTile: { version_number: number; pdf_url: string } | null;
  onVersionClick: (v: number) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      {thoughts.length > 0 && <ThinkingBlock thoughts={thoughts} defaultOpen />}
      {versionTile && (
        <VersionTile
          versionNumber={versionTile.version_number}
          label="Preview ready"
          onClick={() => onVersionClick(versionTile.version_number)}
        />
      )}
      {/* Pulsing dots indicator */}
      <div className="flex justify-start">
        <div className="flex gap-1.5 py-2">
          <span className="h-2 w-2 rounded-full bg-ink/40 animate-bounce [animation-delay:0ms]" />
          <span className="h-2 w-2 rounded-full bg-ink/40 animate-bounce [animation-delay:150ms]" />
          <span className="h-2 w-2 rounded-full bg-ink/40 animate-bounce [animation-delay:300ms]" />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Collapsible thinking block
// ---------------------------------------------------------------------------

function ThinkingBlock({ thoughts, defaultOpen = false }: { thoughts: string[]; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="border-l-2 border-ink/20 pl-3">
      <button
        className="flex items-center gap-1.5 text-xs font-semibold text-ink/50 hover:text-ink/80 transition-colors mb-1"
        onClick={() => setOpen((o) => !o)}
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        Thought for {thoughts.length} step{thoughts.length !== 1 ? "s" : ""}
      </button>
      {open && (
        <ol className="mt-1 flex flex-col gap-1">
          {thoughts.map((t, i) => (
            <li key={i} className="text-xs text-ink/60 leading-relaxed">
              {i + 1}. {t}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Version tile — inline card, clickable to open that version in preview
// ---------------------------------------------------------------------------

function VersionTile({
  versionNumber,
  label,
  onClick,
}: {
  versionNumber: number;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className="flex w-full max-w-[85%] items-center gap-3 border-2 border-ink bg-ink/5 px-4 py-3 text-left hover:bg-ink/10 transition-colors group"
      onClick={onClick}
      title={`Open version ${versionNumber} in preview`}
    >
      <ChevronRight size={14} className="shrink-0 text-ink/50 group-hover:text-ink transition-colors" />
      <FileText size={14} className="shrink-0 text-ink/60" />
      <span className="min-w-0 flex-1 truncate text-sm font-semibold text-ink">
        {label || "Document update"}
      </span>
      <span className="shrink-0 rounded border border-ink/30 px-2 py-0.5 text-xs font-bold text-ink/60">
        v{versionNumber}
      </span>
    </button>
  );
}
