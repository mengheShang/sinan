"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { apiFetch, COOKIE } from "@/lib/session";

export async function logout(): Promise<void> {
  try {
    await apiFetch("/auth/logout", { method: "POST" });
  } catch {
    // The cookie is removed below either way
  }
  const store = await cookies();
  store.delete(COOKIE);
  redirect("/login");
}
