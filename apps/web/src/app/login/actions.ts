"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { apiUrl, COOKIE, readIssuedCookie } from "@/lib/session";

// The username is sent back so the form can keep it after a failed attempt
export type LoginState = { error: string | null; username: string };

export async function login(_prev: LoginState, form: FormData): Promise<LoginState> {
  const username = String(form.get("username") ?? "");
  const password = String(form.get("password") ?? "");
  const fail = (error: string): LoginState => ({ error, username });
  let resp: Response;
  try {
    resp = await fetch(`${apiUrl}/auth/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username, password }),
      cache: "no-store",
    });
  } catch {
    return fail("Cannot reach the API.");
  }
  // Same message for a wrong username or password, matching the API
  if (resp.status === 401) {
    return fail("Wrong username or password.");
  }
  if (resp.status === 429) {
    const wait = resp.headers.get("retry-after") ?? "a few";
    return fail(`Too many attempts. Try again in ${wait} seconds.`);
  }
  const issued = resp.ok ? readIssuedCookie(resp) : null;
  if (!issued) {
    return fail("Login failed. Check the API logs.");
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
