import { create } from "zustand";

// Live streaming state for the in-progress agent message.
export type LiveVersionTile = {
  version_number: number;
  pdf_url: string;
  document_url: string;
};

type AppState = {
  workspaceId: string | null;
  sidebarOpen: boolean;

  // Pin a specific version in the preview panel (overrides current_version).
  pinnedVersion: number | null;

  // Live streaming state (reset at start of each agent run).
  isAgentRunning: boolean;
  liveUserMessage: string | null;
  liveThoughts: string[];
  liveVersionTile: LiveVersionTile | null;
  seenEventCount: number;

  setWorkspaceId: (id: string | null) => void;
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  setPinnedVersion: (version: number | null) => void;

  startAgentRun: (userMessage: string) => void;
  applyPollEvents: (events: any[]) => void;
  endAgentRun: () => void;
};

export const useAppStore = create<AppState>((set) => ({
  workspaceId: null,
  sidebarOpen: false,
  pinnedVersion: null,
  isAgentRunning: false,
  liveUserMessage: null,
  liveThoughts: [],
  liveVersionTile: null,
  seenEventCount: 0,

  setWorkspaceId: (id) => set({ workspaceId: id, pinnedVersion: null }),
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  setPinnedVersion: (version) => set({ pinnedVersion: version }),

  startAgentRun: (userMessage) => set({ 
    isAgentRunning: true, 
    liveUserMessage: userMessage, 
    liveThoughts: [], 
    liveVersionTile: null, 
    pinnedVersion: null,
    seenEventCount: 0
  }),
  applyPollEvents: (events) => set((s) => {
    // Only process new events
    if (events.length <= s.seenEventCount) return s;
    
    const newEvents = events.slice(s.seenEventCount);
    let updatedThoughts = [...s.liveThoughts];
    let updatedVersionTile = s.liveVersionTile;
    
    for (const ev of newEvents) {
      if (ev.type === "thought") {
        updatedThoughts.push(ev.content);
      } else if (ev.type === "version_created") {
        updatedVersionTile = {
          version_number: ev.version_number,
          pdf_url: ev.pdf_url,
          document_url: ev.document_url,
        };
      }
    }
    
    return {
      liveThoughts: updatedThoughts,
      liveVersionTile: updatedVersionTile,
      seenEventCount: events.length
    };
  }),
  endAgentRun: () => set({ isAgentRunning: false, liveUserMessage: null }),
}));
