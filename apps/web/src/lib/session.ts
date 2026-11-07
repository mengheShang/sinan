// Server-side helpers for the session cookie. The browser never calls the API
// directly; the Dashboard server forwards the cookie on each request.
import { cookies } from "next/headers";

export const COOKIE = "sinan_session";
export const apiUrl = process.env.API_URL ?? "http://127.0.0.1:8000";

// Calls the API with the caller's session cookie attached
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const store = await cookies();
  const token = store.get(COOKIE)?.value;
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("cookie", `${COOKIE}=${token}`);
  }
  return fetch(`${apiUrl}${path}`, { ...init, headers, cache: "no-store" });
}

export type IssuedCookie = { value: string; maxAge: number };

// Reads the session cookie the API issued from its Set-Cookie headers
export function readIssuedCookie(resp: Response): IssuedCookie | null {
  for (const line of resp.headers.getSetCookie()) {
    const [pair, ...attrs] = line.split(";");
    const eq = pair.indexOf("=");
    const name = pair.slice(0, eq).trim();
    if (name !== COOKIE) {
      continue;
    }
    const value = pair.slice(eq + 1).trim();
    const maxAgeAttr = attrs.find((a) => a.trim().toLowerCase().startsWith("max-age="));
    const maxAge = maxAgeAttr ? Number(maxAgeAttr.split("=")[1]) : 0;
    return { value, maxAge };
  }
  return null;
}
