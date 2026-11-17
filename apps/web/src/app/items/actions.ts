"use server";

import { refresh } from "next/cache";
import { redirect } from "next/navigation";
import { apiFetch } from "@/lib/session";

const TARGETS = new Set(["open", "done", "dismissed"]);

// Marks an item done or dismissed, or reopens it; local database only
export async function setStatus(form: FormData): Promise<void> {
  const id = String(form.get("id") ?? "");
  const status = String(form.get("status") ?? "");
  if (!id || !TARGETS.has(status)) {
    throw new Error("Bad status change");
  }
  const resp = await apiFetch(`/items/${encodeURIComponent(id)}/status`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (resp.status === 401) {
    redirect("/login");
  }
  // 409 means the item already moved, e.g. a double click; the refresh shows its real state
  if (!resp.ok && resp.status !== 409) {
    throw new Error(`Status change failed (${resp.status})`);
  }
  refresh();
}

// Makes a notice a to-do (ADR 0017); appends an item version, local database only
export async function makeTodo(form: FormData): Promise<void> {
  const id = String(form.get("id") ?? "");
  if (!id) {
    throw new Error("Bad kind change");
  }
  const resp = await apiFetch(`/items/${encodeURIComponent(id)}/kind`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind: "todo" }),
  });
  if (resp.status === 401) {
    redirect("/login");
  }
  // 409 means it is no longer a notice, e.g. a double click; the refresh shows its real state
  if (!resp.ok && resp.status !== 409) {
    throw new Error(`Kind change failed (${resp.status})`);
  }
  refresh();
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Dismisses the notices the page showed, by their IDs (ADR 0017); local database only.
// The API skips any ID that is no longer an open notice.
export async function dismissNotices(form: FormData): Promise<void> {
  const ids = form.getAll("id").map(String);
  if (ids.length === 0 || !ids.every((id) => UUID.test(id))) {
    throw new Error("Bad notice list");
  }
  const resp = await apiFetch("/notices/dismiss", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (resp.status === 401) {
    redirect("/login");
  }
  if (!resp.ok) {
    throw new Error(`Dismiss all failed (${resp.status})`);
  }
  refresh();
}

const WALL = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;

// Sets a review item's due time (ADR 0020): the model's suggestion or the
// parser's time as shown (due_at, UTC), or a wall time the owner typed in the
// owner's zone (local). Appends an item version; local database only.
export async function setDue(form: FormData): Promise<void> {
  const id = String(form.get("id") ?? "");
  const dueAt = String(form.get("due_at") ?? "");
  const local = String(form.get("local") ?? "");
  const body = dueAt ? { due_at: dueAt } : { local };
  const valid = dueAt ? !Number.isNaN(Date.parse(dueAt)) : WALL.test(local);
  if (!id || !valid) {
    throw new Error("Bad due time");
  }
  const resp = await apiFetch(`/items/${encodeURIComponent(id)}/due`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (resp.status === 401) {
    redirect("/login");
  }
  if (resp.status === 422) {
    throw new Error("That time does not exist or happens twice on a clock change");
  }
  // 409 means it no longer waits on its due time, e.g. a double click; the refresh shows it
  if (!resp.ok && resp.status !== 409) {
    throw new Error(`Due time change failed (${resp.status})`);
  }
  refresh();
}
