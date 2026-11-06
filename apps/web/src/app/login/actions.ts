"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { apiUrl, COOKIE, readIssuedCookie } from "@/lib/session";

export type LoginState = { error: string | null };

export async function login(_prev: LoginState, form: FormData): Promise<LoginState> {
  const password = String(form.get("password") ?? "");
  let resp: Response;
  try {
    resp = await fetch(`${apiUrl}/auth/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password }),
      cache: "no-store",
    });
  } catch {
    return { error: "Cannot reach the API." };
  }
  if (resp.status === 401) {
    return { error: "Wrong password." };
  }
  if (resp.status === 429) {
    const wait = resp.headers.get("retry-after") ?? "a few";
    return { error: `Too many attempts. Try again in ${wait} seconds.` };
  }
  const issued = resp.ok ? readIssuedCookie(resp) : null;
  if (!issued) {
    return { error: "Login failed. Check the API logs." };
  }
  const store = await cookies();
  // Plain HTTP on 127.0.0.1, so the cookie cannot be marked Secure
  store.set(COOKIE, issued.value, {
    httpOnly: true,
    sameSite: "strict",
    path: "/",
    maxAge: issued.maxAge,
  });
  redirect("/");
}
