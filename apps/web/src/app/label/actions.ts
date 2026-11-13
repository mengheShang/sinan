"use server";

import { redirect } from "next/navigation";
import { apiFetch } from "@/lib/session";
import type { Problem, SaveState } from "./types";

type Saved = { label_id: string | null; next_open_id: string | null };

function goTo(id: string): never {
  redirect(`/label?id=${encodeURIComponent(id)}`);
}

async function post(path: string, body?: string): Promise<Response> {
  const resp = await apiFetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
  if (resp.status === 401) {
    redirect("/login");
  }
  return resp;
}

// Saves a new label, or a correction when label_id is set. Local database only.
export async function saveLabel(_prev: SaveState, form: FormData): Promise<SaveState> {
  const id = String(form.get("id") ?? "");
  const labelId = String(form.get("label_id") ?? "");
  const payload = String(form.get("payload") ?? "");
  if (!id || !payload) {
    return { problems: [], error: "Nothing to save." };
  }
  const path = labelId
    ? `/labeling/labels/${encodeURIComponent(labelId)}/correct`
    : `/labeling/emails/${encodeURIComponent(id)}/label`;
  const resp = await post(path, payload);
  if (resp.status === 422) {
    const data = (await resp.json()) as { detail: Problem[] };
    return { problems: data.detail, error: "Fix the fields below." };
  }
  if (resp.status === 409) {
    return { problems: [], error: "This email changed meanwhile. Reload the page." };
  }
  if (!resp.ok) {
    return { problems: [], error: `Save failed (${resp.status}).` };
  }
  const saved = (await resp.json()) as Saved;
  // A new label moves on to the next email; a correction stays here
  goTo(labelId ? id : (saved.next_open_id ?? id));
}

// Leaves an email out of the queue and moves on
export async function skipEmail(form: FormData): Promise<void> {
  const id = String(form.get("id") ?? "");
  if (!id) {
    throw new Error("Nothing to skip");
  }
  const resp = await post(`/labeling/emails/${encodeURIComponent(id)}/skip`);
  if (!resp.ok && resp.status !== 409) {
    throw new Error(`Skip failed (${resp.status})`);
  }
  const saved = resp.ok ? ((await resp.json()) as Saved) : null;
  goTo(saved?.next_open_id ?? id);
}
