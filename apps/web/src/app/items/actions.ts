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
