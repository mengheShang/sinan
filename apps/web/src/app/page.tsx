import Link from "next/link";
import { redirect } from "next/navigation";
import { connection } from "next/server";
import { Suspense } from "react";
import { apiFetch, apiUrl } from "@/lib/session";
import { logout } from "./actions";
import { Dashboard } from "./digest";
import styles from "./page.module.css";

type Check = { label: string; ok: boolean; detail: string };

async function probe(label: string, path: string): Promise<Check> {
  try {
    const resp = await fetch(`${apiUrl}${path}`, { cache: "no-store" });
    const body = await resp.json();
    const ok = resp.ok && body.status === "ok";
    return { label, ok, detail: JSON.stringify(body) };
  } catch {
    return { label, ok: false, detail: "unreachable" };
  }
}

// The API is the real check: a missing, expired or tampered cookie gets a 401
async function SignedIn() {
  const resp = await apiFetch("/auth/session");
  if (resp.status === 401) {
    redirect("/login");
  }
  return (
    <form action={logout} className={styles.session}>
      <span>Signed in</span>
      <Link href="/accounts">Accounts</Link>
      <Link href="/items">Closed items</Link>
      <button type="submit">Sign out</button>
    </form>
  );
}

async function Status() {
  await connection();
  const api = await probe("API", "/health");
  const db = await probe("Database", "/health/db");
  const checks = [api, db];
  return (
    <ul className={styles.list}>
      {checks.map((c) => (
        <li key={c.label}>
          <span className={c.ok ? styles.ok : styles.bad}>{c.ok ? "OK" : "Down"}</span>
          <strong>{c.label}</strong>
          <code>{c.detail}</code>
        </li>
      ))}
    </ul>
  );
}

export default function Home() {
  return (
    <main className={styles.main}>
      <h1>Sinan</h1>
      <Suspense fallback={<p>Checking session…</p>}>
        <SignedIn />
      </Suspense>
      <p className={styles.note}>
        Your daily digest. Sinan only reads mail; it is shown here and never sent anywhere.
      </p>
      <Suspense fallback={<p>Loading the digest…</p>}>
        <Dashboard />
      </Suspense>
      <details className={styles.health}>
        <summary>Service health</summary>
        <Suspense fallback={<p>Checking…</p>}>
          <Status />
        </Suspense>
      </details>
    </main>
  );
}
