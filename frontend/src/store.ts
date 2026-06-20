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

  setWorkspaceId: (id: string | null) => void;
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  setPinnedVersion: (version: number | null) => void;

  startAgentRun: (userMessage: string) => void;
  pushThought: (thought: string) => void;
  setLiveVersionTile: (tile: LiveVersionTile) => void;
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

  setWorkspaceId: (id) => set({ workspaceId: id, pinnedVersion: null }),
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  setPinnedVersion: (version) => set({ pinnedVersion: version }),

  startAgentRun: (userMessage) => set({ isAgentRunning: true, liveUserMessage: userMessage, liveThoughts: [], liveVersionTile: null, pinnedVersion: null }),
  pushThought: (thought) =>
    set((s) => ({ liveThoughts: [...s.liveThoughts, thought] })),
  setLiveVersionTile: (tile) => set({ liveVersionTile: tile }),
  endAgentRun: () => set({ isAgentRunning: false, liveUserMessage: null }),
}));
