"use server";

import { redirect } from "next/navigation";
import { apiFetch } from "@/lib/session";

const ROLES = new Set(["school", "personal"]);

// Asks the API for Google's consent page and sends the browser there
export async function connect(form: FormData): Promise<void> {
  const role = String(form.get("role") ?? "");
  if (!ROLES.has(role)) {
    redirect("/accounts?oauth=error");
  }
  const resp = await apiFetch("/oauth/google/start", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ role }),
  });
  if (resp.status === 401) {
    redirect("/login");
  }
  if (!resp.ok) {
    redirect("/accounts?oauth=error");
  }
  const body: { url: string } = await resp.json();
  redirect(body.url);
}

// Tries the stored read token now; an expired one flags the account
export async function check(form: FormData): Promise<void> {
  const id = String(form.get("id") ?? "");
  const resp = await apiFetch(`/accounts/${encodeURIComponent(id)}/check`, { method: "POST" });
  if (resp.status === 401) {
    redirect("/login");
  }
  const result = resp.ok ? "checked" : "check_failed";
  redirect(`/accounts?oauth=${result}`);
}

const PURPOSES = new Set(["write", "calendar"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// "Enable actions" (write) or "Connect calendar" for one account (ADR 0021).
// The API answers with Google's consent page for that account's address.
export async function authorize(form: FormData): Promise<void> {
  const id = String(form.get("id") ?? "");
  const purpose = String(form.get("purpose") ?? "");
  if (!UUID.test(id) || !PURPOSES.has(purpose)) {
    redirect("/accounts?oauth=error");
  }
  const path = `/accounts/${encodeURIComponent(id)}/oauth/${purpose}`;
  const resp = await apiFetch(path, { method: "POST" });
  if (resp.status === 401) {
    redirect("/login");
  }
  if (resp.status === 409) {
    redirect("/accounts?oauth=not_set_up");
  }
  if (!resp.ok) {
    redirect("/accounts?oauth=error");
  }
  const body: { url: string } = await resp.json();
  redirect(body.url);
}
