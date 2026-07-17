import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  FileText,
  Image as ImageIcon,
  Paperclip,
  RotateCcw,
  Send,
  X,
} from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";
import { rollback, startChat, pollRun } from "../api";
import { useAppStore } from "../store";
import type { AgentContent, Workspace } from "../types";
import { KnowledgePanel } from "./KnowledgePanel";

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
    applyPollEvents,
    endAgentRun,
  } = useAppStore();

  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [attachedImage, setAttachedImage] = useState<File | null>(null);
  const [imagePreview, setImagePreview] = useState<string | null>(null);
  const [selectedModel, setSelectedModel] = useState<string>("openrouter-gemini-2.5-flash-lite");
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  
  // Keep track of active poll interval to clear on unmount
  const pollIntervalRef = useRef<number | null>(null);

  // Clear polling on unmount
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
      }
    };
  }, []);

  // ---- Auto-scroll ---------------------------------------------------
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [workspace.messages, liveThoughts, liveUserMessage, isAgentRunning]);

  // ---- Image attachment -----------------------------------------------
  function onAttachClick() {
    fileInputRef.current?.click();
  }

  function onFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setAttachedImage(file);
    const reader = new FileReader();
    reader.onload = () => setImagePreview(reader.result as string);
    reader.readAsDataURL(file);
    // Reset input so the same file can be re-selected
    e.target.value = "";
  }

  function removeAttachment() {
    setAttachedImage(null);
    setImagePreview(null);
  }

  // ---- Chat mutation & Polling ---------------------------------------
  const chatMutation = useMutation({
    mutationFn: ({ content, image, provider, model }: { content: string; image?: File; provider: string; model: string }) =>
      startChat(workspace.id, content, image, provider, model),
    onMutate: ({ content }) => {
      startAgentRun(content);
    },
    onSuccess: (data) => {
      const runId = data.run_id;
      
      // Start polling
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
      
      pollIntervalRef.current = window.setInterval(async () => {
        try {
          const pollRes = await pollRun(runId);
          
          if (pollRes.events) {
            applyPollEvents(pollRes.events);
          }
          
          if (pollRes.status === "completed" || pollRes.status === "error") {
            if (pollIntervalRef.current) {
              clearInterval(pollIntervalRef.current);
              pollIntervalRef.current = null;
            }
            
            if (pollRes.status === "completed" && pollRes.workspace) {
              // Directly apply the serialized workspace to the cache
              queryClient.setQueryData(["workspace", workspace.id], pollRes.workspace);
              queryClient.invalidateQueries({ queryKey: ["workspaces"] });
            }
            
            endAgentRun();
          }
        } catch (err) {
          console.error("Polling error:", err);
          // Keep trying, transient network errors shouldn't break the UI
        }
      }, 1500); // Short-poll every 1.5s
    },
    onError: () => {
      // If the POST /chat itself fails
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
    const img = attachedImage ?? undefined;
    setDraft("");
    setAttachedImage(null);
    setImagePreview(null);
    
    let provider = "openai";
    let actualModel = selectedModel;
    if (selectedModel.startsWith("gemini") && !selectedModel.startsWith("openrouter")) {
      provider = "gemini";
    } else if (selectedModel === "openrouter-gemini-2.5-flash-lite") {
      provider = "openrouter";
      actualModel = "google/gemini-2.5-flash-lite";
    }
    
    chatMutation.mutate({ content, image: img, provider, model: actualModel });
  }

  // ---- Render --------------------------------------------------------
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/svg+xml,image/gif"
        className="hidden"
        onChange={onFileChange}
      />

      {/* Version pills + refresh */}
      <div className="border-b-2 border-ink px-5 py-3">
        <p className="truncate text-xs font-bold text-ink/60 mb-2">{workspace.original_filename}</p>
        <div className="flex flex-wrap gap-1.5">
          {workspace.versions.map((v) => (
            <button
              key={v.id}
              className={`h-7 border-2 border-ink px-2.5 text-xs font-bold transition-colors hover:bg-accent ${
                (pinnedVersion === v.version_number) ||
                (!pinnedVersion && v.version_number === workspace.current_version)
                  ? "bg-accent"
                  : "bg-paper"
              }`}
              onClick={() => setPinnedVersion(v.version_number)}
              title={`Preview version ${v.version_number}`}
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
          {pinnedVersion !== null && pinnedVersion !== workspace.current_version && (
            <button
              className="ml-auto flex h-7 items-center justify-center border-2 border-ink bg-paper px-2.5 text-xs font-bold hover:bg-accent"
              title={`Rollback to version ${pinnedVersion}`}
              onClick={() => {
                rollbackMutation.mutate(pinnedVersion);
                setPinnedVersion(null);
              }}
            >
              Rollback to v{pinnedVersion}
            </button>
          )}
        </div>
      </div>

      {/* Knowledge Base Panel */}
      <KnowledgePanel workspaceId={workspace.id} />

      {/* Message list */}
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="flex flex-col gap-4">
          {workspace.messages.map((message) => {
            if (message.role === "user") {
              return <UserBubble key={message.id} content={message.content} imageUrl={message.image_url} />;
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

          {/* Optimistically rendered user message (with attached image if any) */}
          {isAgentRunning && liveUserMessage && (
            <UserBubble
              content={liveUserMessage}
              imageUrl={imagePreview ?? undefined}
            />
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

      {/* Image preview strip */}
      {imagePreview && (
        <div className="border-t-2 border-ink/20 bg-ink/5 px-4 py-2 flex items-center gap-3">
          <div className="relative w-16 h-16 flex-shrink-0">
            <img
              src={imagePreview}
              alt="Attachment preview"
              className="w-16 h-16 object-cover border-2 border-ink/30"
            />
            <button
              type="button"
              className="absolute -top-1.5 -right-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-ink text-paper hover:bg-ink/70 transition-colors"
              onClick={removeAttachment}
              title="Remove attachment"
            >
              <X size={10} />
            </button>
          </div>
          <div className="flex flex-col min-w-0">
            <span className="text-xs font-semibold text-ink truncate">{attachedImage?.name}</span>
            <span className="text-xs text-ink/50">
              {attachedImage ? (attachedImage.size / 1024).toFixed(1) + " KB" : ""}
            </span>
          </div>
        </div>
      )}

      {/* Input area */}
      <form onSubmit={onSubmit} className="flex flex-col gap-0 border-t-2 border-ink">
        <div className="flex px-4 pt-3 gap-2">
          <select 
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            className="text-xs border-2 border-ink bg-paper px-2 py-1 outline-none focus:bg-accent/30 font-medium cursor-pointer"
            disabled={isAgentRunning}
          >
            <option value="gemini-3.1-flash-lite">Gemini 3.1 Flash Lite</option>
            <option value="openrouter-gemini-2.5-flash-lite">OpenRouter (Gemini 2.5 Flash Lite)</option>
            <option value="gpt-4o-mini">GPT-4o Mini</option>
          </select>
        </div>
        <div className="flex gap-2 p-4 pt-2">
          <textarea
            className="min-h-20 flex-1 resize-none border-2 border-ink bg-paper p-3 text-sm font-medium outline-none focus:bg-accent/30 transition-colors"
            value={draft}
            placeholder={
              attachedImage
                ? "Describe what you'd like to do with this image…"
                : "Ask the agent to edit the document… (attach an image with 📎)"
            }
            disabled={isAgentRunning}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSubmit(e as unknown as FormEvent);
              }
            }}
          />
          <div className="flex flex-col gap-2">
            <button
              type="button"
              className={`flex h-10 w-10 items-center justify-center border-2 border-ink transition-colors ${
                attachedImage
                  ? "bg-accent border-accent/80"
                  : "bg-paper hover:bg-accent/40"
              }`}
              title="Attach an image"
              onClick={onAttachClick}
              disabled={isAgentRunning}
            >
              <Paperclip size={16} className={attachedImage ? "text-ink" : "text-ink/60"} />
            </button>
            <button
              type="submit"
              className="flex flex-1 min-h-10 w-10 items-center justify-center border-2 border-ink bg-accent disabled:opacity-40"
              title="Send"
              disabled={isAgentRunning || !draft.trim()}
            >
              <Send size={18} />
            </button>
          </div>
        </div>

      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Quick-action chip button
// ---------------------------------------------------------------------------

function QuickActionChip({
  label,
  prompt,
  disabled,
  onSelect,
}: {
  label: string;
  prompt: string;
  disabled: boolean;
  onSelect: (prompt: string) => void;
}) {
  return (
    <button
      type="button"
      className="h-6 px-2.5 border border-ink/20 bg-paper text-xs font-medium text-ink/60 hover:bg-accent/40 hover:text-ink hover:border-ink/40 transition-all disabled:opacity-40"
      disabled={disabled}
      onClick={() => onSelect(prompt)}
      title={prompt}
    >
      {label}
    </button>
  );
}

// ---------------------------------------------------------------------------
// User message bubble
// ---------------------------------------------------------------------------

function UserBubble({ content, imageUrl }: { content: string; imageUrl?: string }) {
  return (
    <div className="flex justify-end">
      <div className="flex flex-col items-end gap-2 max-w-[85%]">
        {imageUrl && (
          <div className="border-2 border-ink/30 overflow-hidden rounded-sm">
            <img
              src={imageUrl}
              alt="Attached image"
              className="max-w-48 max-h-48 object-cover"
            />
          </div>
        )}
        <div className="rounded-2xl rounded-tr-sm bg-ink px-4 py-3 text-paper text-sm font-medium shadow-sm">
          {content}
        </div>
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
    return (
      <div className="flex justify-start">
        <div className="max-w-[85%] text-sm text-ink/70 font-medium py-1">{rawContent}</div>
      </div>
    );
  }

  // Special "needs image" card
  if (content.needs_image) {
    return (
      <div className="flex flex-col gap-2">
        {content.thoughts.length > 0 && <ThinkingBlock thoughts={content.thoughts} />}
        <NeedsImageCard message={content.text} />
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
// Needs-image prompt card
// ---------------------------------------------------------------------------

function NeedsImageCard({ message }: { message: string }) {
  return (
    <div className="flex justify-start">
      <div className="flex max-w-[85%] items-start gap-3 border-2 border-amber-400/60 bg-amber-50/60 px-4 py-3">
        <AlertCircle size={16} className="shrink-0 text-amber-500 mt-0.5" />
        <div className="flex flex-col gap-1">
          <p className="text-sm font-semibold text-ink">Image Required</p>
          <p className="text-sm text-ink/70 leading-relaxed">{message}</p>
          <div className="mt-1 flex items-center gap-1.5 text-xs text-ink/50">
            <ImageIcon size={12} />
            <span>Click the 📎 paperclip icon to attach your image</span>
          </div>
        </div>
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
