export type AgentContent = {
  type: "agent_response";
  thoughts: string[];
  text: string;
  version_number: number | null;
  version_label: string | null;
  needs_image?: boolean;
};

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  content_parsed: AgentContent | null;
  image_url?: string;
  created_at: string;
};

export type Version = {
  id: string;
  version_number: number;
  document_url: string;
  pdf_url: string;
  created_at: string;
};

export type Workspace = {
  id: string;
  document_type: "pptx" | "docx";
  original_filename: string;
  current_version: number;
  created_at: string;
  updated_at: string;
  messages: Message[];
  versions: Version[];
};

export type SocketEvent =
  | { type: "thought"; content: string; iteration: number }
  | { type: "version_created"; version_number: number; pdf_url: string; document_url: string; iteration: number }
  | { type: "completed"; version: number | null }
  | { type: "error"; message: string };
