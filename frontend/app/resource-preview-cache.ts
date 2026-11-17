"use client";

const MAX_BYTES = 200 * 1024 * 1024;
const MAX_AGE = 7 * 86400000;
type Entry = { key: string; user: string; blob: Blob; size: number; at: number };
const memory = new Map<string, Entry>();
let database: Promise<IDBDatabase> | undefined;
let epoch = 0;
let writes: Promise<void> = Promise.resolve();

function serialize(operation: () => Promise<void>) {
  writes = writes.catch(() => {}).then(operation);
  return writes;
}

function open() {
  if (!database) database = new Promise((resolve, reject) => {
    const request = indexedDB.open("zhiku-resource-previews", 2);
    request.onupgradeneeded = () => {
      const db = request.result;
      // This is a disposable cache; discard the old layout without loading its blobs.
      if (db.objectStoreNames.contains("content")) db.deleteObjectStore("content");
      db.createObjectStore("content", { keyPath: "key" });
      db.createObjectStore("metadata", { keyPath: "key" }).createIndex("user", "user");
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return database;
}

async function entries(user?: string): Promise<Omit<Entry, "blob">[]> {
  const db = await open();
  return new Promise((resolve, reject) => {
    const store = db.transaction("metadata").objectStore("metadata");
    const result = user ? store.index("user").getAll(user) : store.getAll();
    result.onsuccess = () => resolve(result.result); result.onerror = () => reject(result.error);
  });
}

function persist(entry: Entry, expectedEpoch: number) {
  return serialize(async () => {
  if (epoch !== expectedEpoch) return;
  const db = await open();
  const all = (await entries(entry.user)).filter(item => item.key !== entry.key);
  if (epoch !== expectedEpoch) return;
  let size = all.reduce((sum, item) => sum + item.size, entry.size);
  const remove = all.sort((a, b) => a.at - b.at).filter(item => {
    if (Date.now() - item.at > MAX_AGE || size > MAX_BYTES) { size -= item.size; return true; }
    return false;
  });
  await new Promise<void>((resolve, reject) => {
    const transaction = db.transaction(["content", "metadata"], "readwrite"), store = transaction.objectStore("content"), metadata = transaction.objectStore("metadata");
    remove.forEach(item => { store.delete(item.key); metadata.delete(item.key); }); store.put(entry);
    metadata.put({ key: entry.key, user: entry.user, size: entry.size, at: entry.at });
    transaction.oncomplete = () => resolve(); transaction.onerror = () => reject(transaction.error);
  });
  });
}

export async function clearPreviewCache(user?: string) {
  epoch++;
  for (const [key, value] of memory) if (!user || value.user === user) memory.delete(key);
  try {
    await serialize(async () => {
      const all = await entries(user), db = await open();
      await new Promise<void>((resolve, reject) => {
        const transaction = db.transaction(["content", "metadata"], "readwrite");
        for (const item of all) {
          transaction.objectStore("content").delete(item.key);
          transaction.objectStore("metadata").delete(item.key);
        }
        transaction.oncomplete = () => resolve(); transaction.onerror = () => reject(transaction.error);
      });
    });
  } catch { /* Private browsing and full disks fall back to memory. */ }
}

export async function cachedPreview(scope: string, revision: string, identity: string, loadContent: () => Promise<Blob>, range?: [number, number]): Promise<Blob> {
  const user = scope.split(":")[0];
  const key = `${scope}:${revision}:${identity}:${range?.join("-") ?? "all"}`;
  const operationEpoch = epoch;
  const hit = memory.get(key);
  if (hit && Date.now() - hit.at < MAX_AGE) {
    hit.at = Date.now(); memory.delete(key); memory.set(key, hit);
    void persist(hit, operationEpoch).catch(() => {}); return hit.blob;
  }
  const load = async () => {
    try {
      const db = await open();
      const entry = await new Promise<Entry | undefined>((resolve, reject) => {
        const result = db.transaction("content").objectStore("content").get(key);
        result.onsuccess = () => resolve(result.result); result.onerror = () => reject(result.error);
      });
      if (entry && Date.now() - entry.at < MAX_AGE && epoch === operationEpoch) {
        entry.at = Date.now(); void persist(entry, operationEpoch).catch(() => {}); return entry.blob;
      }
    } catch { /* Persistent storage is optional; authorization happens before this function. */ }
    const blob = await loadContent();
    if (range && blob.size !== range[1]-range[0]) throw new Error("文件未完整接收，请重试");
    if (blob.size <= MAX_BYTES && operationEpoch === epoch) {
      const entry = { key, user, blob, size: blob.size, at: Date.now() };
      memory.set(key, entry);
      let size = [...memory.values()].reduce((sum, item) => sum + item.size, 0);
      for (const [old, value] of memory) {
        if (size <= MAX_BYTES) break;
        size -= value.size; memory.delete(old);
      }
      void persist(entry, operationEpoch).catch(() => {});
    }
    return blob;
  };
  return load();
}
