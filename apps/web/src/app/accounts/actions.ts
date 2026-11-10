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
