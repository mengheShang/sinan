"use server";

import { refresh } from "next/cache";
import { redirect } from "next/navigation";
import { apiFetch } from "@/lib/session";
import type { DecideState } from "./types";

const HASH = /^[0-9a-f]{64}$/;

// Why the API refused, in the owner's words
const REFUSED: Record<string, string> = {
  "Proposal is expired": "This change expired unanswered; the item now needs review.",
  "Proposal is superseded": "The item changed since this was proposed, so it no longer applies.",
  "Proposal is approved": "This change was already approved.",
  "Proposal is executed": "This change was already applied.",
  "Proposal is rejected": "This change was already rejected.",
  "Proposal does not match": "This change does not match what was shown, so it was not applied.",
};

async function refusal(resp: Response): Promise<string> {
  try {
    const body = (await resp.json()) as { detail?: unknown };
    const detail = typeof body.detail === "string" ? body.detail : "";
    return REFUSED[detail] ?? "This change can no longer be decided.";
  } catch {
    return "This change can no longer be decided.";
  }
}

// Approves or rejects one proposed change. Local database only: the API applies
// an approved change to the item, and nothing is sent anywhere.
export async function decide(_prev: DecideState, form: FormData): Promise<DecideState> {
  const id = String(form.get("id") ?? "");
  const decision = String(form.get("decision") ?? "");
  const hash = String(form.get("payload_hash") ?? "");
  if (!id || (decision !== "approve" && decision !== "reject")) {
    return { message: "Nothing to decide." };
  }
  if (decision === "approve" && !HASH.test(hash)) {
    return { message: "Nothing to approve." };
  }
  const body = decision === "approve" ? JSON.stringify({ payload_hash: hash }) : undefined;
  const resp = await apiFetch(`/proposals/${encodeURIComponent(id)}/${decision}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
  if (resp.status === 401) {
    redirect("/login");
  }
  if (resp.status === 409 || resp.status === 404) {
    return { message: `${await refusal(resp)} Reload to see the current state.` };
  }
  if (!resp.ok) {
    return { message: `Could not save the decision (${resp.status}).` };
  }
  refresh();
  return { message: "" };
}
