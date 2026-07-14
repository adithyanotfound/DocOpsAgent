import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  FileText,
  Loader2,
  Trash2,
  Upload,
} from "lucide-react";
import { useCallback, useRef, useState } from "react";
import {
  deleteKnowledgeDocument,
  getKnowledgeDocumentStatus,
  listKnowledgeDocuments,
  uploadKnowledgeDocument,
} from "../api";
import type { KnowledgeDocument } from "../types";

type Props = {
  workspaceId: string;
};

const FILE_TYPE_ICONS: Record<string, string> = {
  pdf: "📄",
  docx: "📝",
  txt: "📃",
  md: "📋",
};

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

function StatusBadge({ status }: { status: KnowledgeDocument["status"] }) {
  if (status === "processing") {
    return (
      <span className="kb-status-badge kb-status-processing">
        <Loader2 size={10} className="animate-spin" />
        Indexing
      </span>
    );
  }
  if (status === "indexed") {
    return (
      <span className="kb-status-badge kb-status-indexed">
        <CheckCircle2 size={10} />
        Ready
      </span>
    );
  }
  return (
    <span className="kb-status-badge kb-status-failed">
      <AlertCircle size={10} />
      Failed
    </span>
  );
}

export function KnowledgePanel({ workspaceId }: Props) {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const { data: docs = [], isLoading } = useQuery({
    queryKey: ["knowledge", workspaceId],
    queryFn: () => listKnowledgeDocuments(workspaceId),
    refetchInterval: (query) => {
      // Auto-refresh while any doc is processing
      const docs = query.state.data ?? [];
      const hasProcessing = docs.some((d: KnowledgeDocument) => d.status === "processing");
      return hasProcessing ? 3000 : false;
    },
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) => uploadKnowledgeDocument(workspaceId, file),
    onSuccess: () => {
      setUploadError(null);
      queryClient.invalidateQueries({ queryKey: ["knowledge", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
    },
    onError: (err: Error) => {
      setUploadError(err.message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (docId: string) => deleteKnowledgeDocument(workspaceId, docId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["knowledge", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
    },
  });

  const handleFiles = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      setUploadError(null);
      Array.from(files).forEach((file) => {
        const suffix = file.name.split(".").pop()?.toLowerCase() ?? "";
        if (!["pdf", "docx", "txt", "md"].includes(suffix)) {
          setUploadError(`"${file.name}" is not a supported type (PDF, DOCX, TXT, MD)`);
          return;
        }
        uploadMutation.mutate(file);
      });
    },
    [uploadMutation]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragging(false);
      handleFiles(e.dataTransfer.files);
    },
    [handleFiles]
  );

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => setIsDragging(false);

  const indexedCount = docs.filter((d) => d.status === "indexed").length;

  return (
    <div className="kb-panel">
      {/* Header */}
      <div 
        className="kb-panel-header"
        onClick={() => setIsExpanded(!isExpanded)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && setIsExpanded(!isExpanded)}
      >
        <div className="kb-panel-title">
          {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <BookOpen size={14} />
          <span>Knowledge Base</span>
        </div>
        <span className="kb-doc-count">
          {indexedCount}/{docs.length} indexed
        </span>
      </div>

      {/* Collapsible Content */}
      {isExpanded && (
        <div className="kb-panel-content">
          {/* Upload area */}
          <div
            className={`kb-dropzone ${isDragging ? "kb-dropzone-active" : ""}`}
            onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onClick={() => fileInputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && fileInputRef.current?.click()}
        aria-label="Upload knowledge base documents"
      >
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          accept=".pdf,.docx,.txt,.md"
          multiple
          onChange={(e) => handleFiles(e.currentTarget.files)}
          id="kb-file-input"
        />
        {uploadMutation.isPending ? (
          <Loader2 size={18} className="animate-spin kb-upload-icon" />
        ) : (
          <Upload size={18} className="kb-upload-icon" />
        )}
        <p className="kb-dropzone-title">
          {uploadMutation.isPending ? "Uploading…" : "Drop files or click to upload"}
        </p>
        <p className="kb-dropzone-hint">PDF, DOCX, TXT, MD · Max 50 MB</p>
      </div>

      {uploadError && (
        <div className="kb-error">
          <AlertCircle size={12} />
          <span>{uploadError}</span>
        </div>
      )}

      {/* Document list */}
      {isLoading ? (
        <div className="kb-loading">
          <Loader2 size={14} className="animate-spin" />
          <span>Loading…</span>
        </div>
      ) : docs.length === 0 ? (
        <div className="kb-empty">
          <BookOpen size={28} className="kb-empty-icon" />
          <p className="kb-empty-title">No documents yet</p>
          <p className="kb-empty-hint">
            Upload research papers, reports, or any documents to ground
            AI-generated content in your data.
          </p>
        </div>
      ) : (
        <div className="kb-doc-list">
          {docs.map((doc) => (
            <div key={doc.id} className="kb-doc-item group">
              <span className="kb-doc-icon">
                {FILE_TYPE_ICONS[doc.file_type] ?? "📄"}
              </span>
              <div className="kb-doc-info">
                <p className="kb-doc-name" title={doc.filename}>
                  {doc.filename}
                </p>
                <div className="kb-doc-meta">
                  <StatusBadge status={doc.status} />
                  <span className="kb-doc-size">{formatBytes(doc.file_size_bytes)}</span>
                  {doc.status === "indexed" && doc.chunk_count > 0 && (
                    <span className="kb-doc-chunks">{doc.chunk_count} chunks</span>
                  )}
                </div>
                {doc.status === "failed" && doc.error_message && (
                  <p className="kb-doc-error">{doc.error_message}</p>
                )}
              </div>
              <button
                className="kb-doc-delete"
                onClick={() => deleteMutation.mutate(doc.id)}
                disabled={deleteMutation.isPending}
                title={`Remove ${doc.filename}`}
                id={`kb-delete-${doc.id}`}
                aria-label={`Delete ${doc.filename}`}
              >
                <Trash2 size={12} />
              </button>
            </div>
          ))}
        </div>
      )}

          {/* Footer hint */}
          {docs.length > 0 && (
            <p className="kb-footer-hint">
              Documents in the knowledge base will be used to ground generated content.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
