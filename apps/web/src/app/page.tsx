import { connection } from "next/server";
import { Suspense } from "react";
import styles from "./page.module.css";

// 在服务端请求 API，浏览器不直接访问 API，因此不需要 CORS
const apiUrl = process.env.API_URL ?? "http://127.0.0.1:8000";

type Check = { label: string; ok: boolean; detail: string };

async function probe(label: string, path: string): Promise<Check> {
  try {
    const resp = await fetch(`${apiUrl}${path}`, { cache: "no-store" });
    const body = await resp.json();
    const ok = resp.ok && body.status === "ok";
    return { label, ok, detail: JSON.stringify(body) };
  } catch {
    return { label, ok: false, detail: "无法连接" };
  }
}

async function Status() {
  await connection();
  const api = await probe("API", "/health");
  const db = await probe("数据库", "/health/db");
  const checks = [api, db];
  return (
    <ul className={styles.list}>
      {checks.map((c) => (
        <li key={c.label}>
          <span className={c.ok ? styles.ok : styles.bad}>{c.ok ? "正常" : "异常"}</span>
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
      <p>M0 骨架：下面是各服务的健康检查结果。</p>
      <Suspense fallback={<p>检查中…</p>}>
        <Status />
      </Suspense>
    </main>
  );
}
