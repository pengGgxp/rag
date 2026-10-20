"use client";

import { FormEvent, useEffect, useState } from "react";

type KnowledgeBase = {
  id: string;
  name: string;
  description?: string | null;
  status: string;
  document_count: number;
};

type Document = {
  id: string;
  filename: string;
  status: string;
  content_type: string;
  size_bytes: number;
  job?: { state: string; error_detail?: string | null };
};

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function Home() {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [documents, setDocuments] = useState<Document[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function loadKnowledgeBases() {
    const response = await fetch(`${API_URL}/knowledge-bases`);
    if (!response.ok) throw new Error("Unable to load knowledge bases");
    const data = (await response.json()) as KnowledgeBase[];
    setKnowledgeBases(data);
    if (!selectedId && data[0]) setSelectedId(data[0].id);
  }

  async function loadDocuments(id: string) {
    const response = await fetch(`${API_URL}/knowledge-bases/${id}/documents`);
    if (response.ok) setDocuments((await response.json()) as Document[]);
  }

  useEffect(() => {
    loadKnowledgeBases().catch((error: Error) => setMessage(error.message));
  }, []);

  useEffect(() => {
    if (selectedId) loadDocuments(selectedId).catch((error: Error) => setMessage(error.message));
  }, [selectedId]);

  async function createKnowledgeBase(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`${API_URL}/knowledge-bases`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, description }),
      });
      if (!response.ok) throw new Error((await response.json()).detail ?? "Could not create knowledge base");
      setName("");
      setDescription("");
      await loadKnowledgeBases();
      setMessage("Knowledge base created.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  async function uploadDocument(event: FormEvent) {
    event.preventDefault();
    if (!selectedId || !file) return;
    setBusy(true);
    setMessage("");
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch(`${API_URL}/knowledge-bases/${selectedId}/documents`, {
        method: "POST",
        body: form,
      });
      if (!response.ok) throw new Error((await response.json()).detail ?? "Upload failed");
      setFile(null);
      await loadDocuments(selectedId);
      await loadKnowledgeBases();
      setMessage("Document uploaded and queued for processing.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  async function updateDocument(document: Document, action: "delete" | "restore") {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`${API_URL}/documents/${document.id}${action === "restore" ? "/restore" : ""}`, {
        method: action === "delete" ? "DELETE" : "POST",
      });
      if (!response.ok) throw new Error((await response.json()).detail ?? "Document update failed");
      await loadDocuments(selectedId);
      await loadKnowledgeBases();
      setMessage(action === "delete" ? "Document deleted." : "Document restored.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Document update failed");
    } finally {
      setBusy(false);
    }
  }

  const selected = knowledgeBases.find((item) => item.id === selectedId);

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <span className="eyebrow">RAG KNOWLEDGE WORKSPACE</span>
          <h1>Build knowledge you can trust.</h1>
          <p className="lede">Create a knowledge base, add source material, and watch its processing state.</p>
        </div>
        <span className="status-dot">LOCAL DOCKER</span>
      </header>

      <section className="grid">
        <aside className="panel sidebar">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">WORKSPACES</span>
              <h2>Knowledge bases</h2>
            </div>
            <span className="count">{knowledgeBases.length}</span>
          </div>
          <div className="list">
            {knowledgeBases.map((item) => (
              <button
                className={`list-item ${item.id === selectedId ? "selected" : ""}`}
                key={item.id}
                onClick={() => setSelectedId(item.id)}
              >
                <span>{item.name}</span>
                <small>{item.document_count} documents</small>
              </button>
            ))}
            {!knowledgeBases.length && <p className="muted">Create your first knowledge base.</p>}
          </div>
          <form className="stack create-form" onSubmit={createKnowledgeBase}>
            <h3>New knowledge base</h3>
            <input required value={name} onChange={(event) => setName(event.target.value)} placeholder="Product docs" />
            <textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What belongs here?" rows={3} />
            <button className="primary" disabled={busy} type="submit">Create knowledge base</button>
          </form>
        </aside>

        <section className="panel content">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">SOURCE LIBRARY</span>
              <h2>{selected?.name ?? "Select a knowledge base"}</h2>
            </div>
            {selected && <span className={`pill ${selected.status}`}>{selected.status}</span>}
          </div>
          {selected ? (
            <>
              <p className="description">{selected.description || "Add source material to begin building this knowledge base."}</p>
              <form className="upload" onSubmit={uploadDocument}>
                <label htmlFor="file">Upload a source document</label>
                <div className="upload-row">
                  <input id="file" type="file" accept=".pdf,.docx,.md,.txt,.html,.csv,.xlsx" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
                  <button className="primary" disabled={busy || !file} type="submit">Upload</button>
                </div>
                <small>PDF, DOCX, Markdown, TXT, HTML, CSV, XLSX · max 100 MB</small>
              </form>
              <div className="documents">
                <div className="section-title"><h3>Documents</h3><button className="ghost" onClick={() => loadDocuments(selectedId)}>Refresh</button></div>
                {documents.map((document) => (
                  <article className="document" key={document.id}>
                    <div><strong>{document.filename}</strong><small>{Math.ceil(document.size_bytes / 1024)} KB · {document.content_type}</small></div>
                    <div className="document-actions">
                      <span className={`pill ${document.status}`}>{document.job?.state ?? document.status}</span>
                      {document.status === "deleted" ? (
                        <button className="ghost" disabled={busy} onClick={() => updateDocument(document, "restore")}>Restore</button>
                      ) : (
                        <button className="ghost danger" disabled={busy} onClick={() => updateDocument(document, "delete")}>Delete</button>
                      )}
                    </div>
                  </article>
                ))}
                {!documents.length && <p className="muted">No documents yet.</p>}
              </div>
            </>
          ) : <p className="empty">Create or select a knowledge base to begin.</p>}
          {message && <p className="notice">{message}</p>}
        </section>
      </section>
    </main>
  );
}

