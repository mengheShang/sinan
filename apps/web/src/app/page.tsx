import { connection } from "next/server";
import { Suspense } from "react";
import styles from "./page.module.css";

// The API is called from the server, never from the browser, so no CORS is needed
const apiUrl = process.env.API_URL ?? "http://127.0.0.1:8000";

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
      <p>M0 skeleton: health checks for each service.</p>
      <Suspense fallback={<p>Checking…</p>}>
        <Status />
      </Suspense>
    </main>
  );
}
