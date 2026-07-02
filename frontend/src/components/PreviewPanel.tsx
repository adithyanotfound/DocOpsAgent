import { ChevronLeft, ChevronRight, Download, Pin, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import type { PDFDocumentProxy } from "pdfjs-dist";
import type { Workspace } from "../types";
import { fileUrl } from "../api";
import { useAppStore } from "../store";

pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.mjs", import.meta.url).toString();

type Props = {
  workspace: Workspace | null;
};

export function PreviewPanel({ workspace }: Props) {
  const { pinnedVersion, setPinnedVersion, isAgentRunning } = useAppStore();

  const displayVersion = useMemo(() => {
    if (!workspace) return null;
    // If the user has pinned a version (by clicking a tile), show that version.
    const pinTarget = pinnedVersion ?? workspace.current_version;
    return workspace.versions.find((v) => v.version_number === pinTarget)
      ?? workspace.versions.find((v) => v.version_number === workspace.current_version)
      ?? null;
  }, [workspace, pinnedVersion]);

  const pdfPath = displayVersion ? fileUrl(displayVersion.pdf_url) : null;
  const isPinned = pinnedVersion !== null && pinnedVersion !== workspace?.current_version;

  return (
    <div className="flex h-full min-h-0 flex-col relative">
      <header className="flex h-16 items-center justify-between border-b-2 border-ink px-5">
        <div>
          <h2 className="text-base font-bold">Preview</h2>
          <p className="text-xs font-medium flex items-center gap-1.5">
            {displayVersion
              ? `Version ${displayVersion.version_number}${isPinned ? " (pinned)" : ""}`
              : "No document loaded"}
            {isPinned && (
              <button
                className="text-ink/50 hover:text-ink underline text-xs"
                onClick={() => setPinnedVersion(null)}
                title="Unpin and return to current version"
              >
                ↩ current
              </button>
            )}
          </p>
        </div>
        {displayVersion && (
          <a
            className="flex h-10 w-10 items-center justify-center border-2 border-ink bg-accent hover:opacity-80"
            href={displayVersion.document_url}
            title="Download document"
          >
            <Download size={18} />
          </a>
        )}
      </header>
      {pdfPath ? (
        <div className="relative flex-1 min-h-0 min-w-0 flex flex-col">
          <PdfCanvas url={pdfPath} />
          {isAgentRunning && <SkeletonOverlay />}
        </div>
      ) : (
        <EmptyPreview />
      )}
    </div>
  );
}

function SkeletonOverlay() {
  return (
    <div className="absolute inset-0 z-10 flex flex-col items-center justify-center bg-paper/80 backdrop-blur-sm p-8">
      <div className="w-full max-w-lg border-2 border-ink/20 bg-paper p-8 shadow-sm">
        <div className="flex animate-pulse space-x-4">
          <div className="flex-1 space-y-6 py-1">
            <div className="h-4 w-3/4 rounded bg-ink/20"></div>
            <div className="space-y-3">
              <div className="h-3 rounded bg-ink/20"></div>
              <div className="h-3 w-5/6 rounded bg-ink/20"></div>
              <div className="h-3 w-4/6 rounded bg-ink/20"></div>
            </div>
            <div className="h-4 w-1/2 rounded bg-ink/20"></div>
            <div className="space-y-3">
              <div className="h-3 rounded bg-ink/20"></div>
              <div className="h-3 rounded bg-ink/20"></div>
              <div className="h-3 w-4/6 rounded bg-ink/20"></div>
            </div>
          </div>
        </div>
      </div>
      <p className="mt-6 font-bold text-ink/60 animate-pulse">Agent is updating document...</p>
    </div>
  );
}

function EmptyPreview() {
  return (
    <div className="flex flex-1 items-center justify-center p-8 text-center">
      <p className="max-w-sm text-sm font-semibold">Upload a document to see the live PDF preview here.</p>
    </div>
  );
}

function PdfCanvas({ url }: { url: string }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [zoom, setZoom] = useState(0.75);
  const [status, setStatus] = useState("Loading preview...");

  useEffect(() => {
    let cancelled = false;
    setStatus("Loading preview...");
    setDoc(null);
    setPageNumber(1);
    pdfjs
      .getDocument(url)
      .promise.then((loaded) => {
        if (!cancelled) {
          setDoc(loaded);
          setStatus("");
        }
      })
      .catch((error: Error) => {
        if (!cancelled) setStatus(error.message);
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  useEffect(() => {
    if (!doc || !canvasRef.current) return;
    let cancelled = false;
    doc.getPage(pageNumber).then((page) => {
      if (cancelled || !canvasRef.current) return;
      const viewport = page.getViewport({ scale: zoom * 1.35 });
      const canvas = canvasRef.current;
      const context = canvas.getContext("2d");
      if (!context) return;
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      page.render({ canvasContext: context, viewport });
    });
    return () => {
      cancelled = true;
    };
  }, [doc, pageNumber, zoom]);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col h-full w-full">
      <div className="flex h-12 items-center justify-between border-b-2 border-ink px-4">
        <div className="flex items-center gap-2">
          <button className="h-8 w-8 border-2 border-ink bg-paper" title="Previous page" onClick={() => setPageNumber((value) => Math.max(1, value - 1))}>
            <ChevronLeft size={16} className="mx-auto" />
          </button>
          <span className="min-w-24 text-center text-xs font-bold">
            {doc ? `${pageNumber} / ${doc.numPages}` : "-"}
          </span>
          <button className="h-8 w-8 border-2 border-ink bg-paper" title="Next page" onClick={() => doc && setPageNumber((value) => Math.min(doc.numPages, value + 1))}>
            <ChevronRight size={16} className="mx-auto" />
          </button>
        </div>
        <div className="flex items-center gap-2">
          <button className="h-8 w-8 border-2 border-ink bg-paper" title="Zoom out" onClick={() => setZoom((value) => Math.max(0.6, value - 0.1))}>
            <ZoomOut size={16} className="mx-auto" />
          </button>
          <span className="w-14 text-center text-xs font-bold">{Math.round(zoom * 100)}%</span>
          <button className="h-8 w-8 border-2 border-ink bg-paper" title="Zoom in" onClick={() => setZoom((value) => Math.min(2.2, value + 0.1))}>
            <ZoomIn size={16} className="mx-auto" />
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto bg-paper p-5">
        {status ? <p className="text-sm font-semibold">{status}</p> : <canvas ref={canvasRef} className="mx-auto border-2 border-ink bg-paper" />}
      </div>
    </div>
  );
}
