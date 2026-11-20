"use server";

// Set or change a due time, and confirm an item (ADR 0022, decision 3).
// Local database only: the API appends an item version, and nothing is sent
// anywhere.
import { refresh } from "next/cache";
import { redirect } from "next/navigation";
import { dueBody, type DueState } from "@/lib/due";
import { apiFetch } from "@/lib/session";

async function detail(resp: Response, fallback: string): Promise<string> {
  try {
    const body = (await resp.json()) as { detail?: unknown };
    return typeof body.detail === "string" ? body.detail : fallback;
  } catch {
    return fallback;
  }
}

export async function saveDue(_prev: DueState, form: FormData): Promise<DueState> {
  const id = String(form.get("id") ?? "");
  const body = dueBody(String(form.get("date") ?? ""), String(form.get("time") ?? ""));
  if (!id) {
    return { message: "Nothing to save." };
  }
  if (body === null) {
    return { message: "Enter a date, and a time like 17:00 if there is one." };
  }
  const resp = await apiFetch(`/items/${encodeURIComponent(id)}/due`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (resp.status === 401) {
    redirect("/login");
  }
  if (resp.status === 409 || resp.status === 422) {
    return { message: await detail(resp, "That due time was not accepted.") };
  }
  if (!resp.ok) {
    return { message: `Could not save the due time (${resp.status}).` };
  }
  refresh();
  return { message: "" };
}

export async function confirmItem(_prev: DueState, form: FormData): Promise<DueState> {
  const id = String(form.get("id") ?? "");
  if (!id) {
    return { message: "Nothing to confirm." };
  }
  const resp = await apiFetch(`/items/${encodeURIComponent(id)}/confirm`, { method: "POST" });
  if (resp.status === 401) {
    redirect("/login");
  }
  if (resp.status === 409) {
    const text = await detail(resp, "This item cannot be confirmed.");
    return { message: `${text} Reload to see the current state.` };
  }
  if (!resp.ok) {
    return { message: `Could not confirm (${resp.status}).` };
  }
  refresh();
  return { message: "" };
}
