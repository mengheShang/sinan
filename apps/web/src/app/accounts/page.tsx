import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import { Suspense } from "react";
import { apiFetch } from "@/lib/session";
import { check, connect } from "./actions";
import styles from "./accounts.module.css";

export const metadata: Metadata = { title: "Accounts · Sinan" };

type Account = {
  id: string;
  email: string;
  role: "school" | "personal";
  authorized_at: string | null;
  expires_at: string | null;
  status: "ok" | "expiring" | "expired" | "needs_reauth";
  last_error: string | null;
};

const MESSAGES: Record<string, string> = {
  connected: "Account connected with read-only access.",
  denied: "You declined access on Google's page. Nothing was stored.",
  scope: "Google did not grant exactly read-only Gmail access, so nothing was stored.",
  expired: "That connect request expired or was already used. Try again.",
  error: "Connecting failed. Check the API logs and try again.",
  checked: "Token checked.",
  check_failed: "Could not reach Google to check the token. Try again later.",
};

const STATUS_TEXT: Record<Account["status"], string> = {
  ok: "Connected",
  expiring: "Expires soon: reconnect",
  expired: "Expired: reconnect",
  needs_reauth: "Needs reconnect",
};

function utc(value: string | null): string {
  if (!value) {
    return "–";
  }
  const iso = new Date(value).toISOString();
  return `${iso.slice(0, 16).replace("T", " ")} UTC`;
}

async function Banner({ searchParams }: { searchParams: Promise<Record<string, unknown>> }) {
  const { oauth } = await searchParams;
  const text = typeof oauth === "string" ? MESSAGES[oauth] : undefined;
  if (!text) {
    return null;
  }
  const good = oauth === "connected" || oauth === "checked";
  return (
    <p role="status" className={good ? styles.good : styles.bad}>
      {text}
    </p>
  );
}

function RoleForm({ role, label }: { role?: Account["role"]; label: string }) {
  return (
    <form action={connect} className={styles.inline}>
      {role ? (
        <input type="hidden" name="role" value={role} />
      ) : (
        <select name="role" defaultValue="school" aria-label="Account role">
          <option value="school">School</option>
          <option value="personal">Personal</option>
        </select>
      )}
      <button type="submit">{label}</button>
    </form>
  );
}

async function AccountList() {
  const resp = await apiFetch("/accounts");
  if (resp.status === 401) {
    redirect("/login");
  }
  if (!resp.ok) {
    return <p className={styles.bad}>Could not load accounts.</p>;
  }
  const accounts: Account[] = await resp.json();
  if (accounts.length === 0) {
    return <p>No accounts connected yet.</p>;
  }
  return (
    <ul className={styles.list}>
      {accounts.map((a) => (
        <li key={a.id} className={styles.card}>
          <div className={styles.row}>
            <strong>{a.email}</strong>
            <span className={styles.role}>{a.role}</span>
            <span className={styles[a.status]}>{STATUS_TEXT[a.status]}</span>
          </div>
          <dl className={styles.meta}>
            <dt>Authorized</dt>
            <dd>{utc(a.authorized_at)}</dd>
            <dt>Expires</dt>
            <dd>{utc(a.expires_at)}</dd>
            {a.last_error ? (
              <>
                <dt>Last error</dt>
                <dd>
                  <code>{a.last_error}</code>
                </dd>
              </>
            ) : null}
          </dl>
          <div className={styles.row}>
            <RoleForm role={a.role} label="Reconnect" />
            <form action={check}>
              <input type="hidden" name="id" value={a.id} />
              <button type="submit">Check token</button>
            </form>
          </div>
        </li>
      ))}
    </ul>
  );
}

export default function AccountsPage({ searchParams }: PageProps<"/accounts">) {
  return (
    <main className={styles.main}>
      <p>
        <Link href="/">← Sinan</Link>
      </p>
      <h1>Accounts</h1>
      <p>
        Sinan reads mail with read-only Gmail access. Google ends this access after 7 days
        while the app is in Testing mode, so reconnect about once a week.
      </p>
      <Suspense fallback={null}>
        <Banner searchParams={searchParams} />
      </Suspense>
      <Suspense fallback={<p>Loading accounts…</p>}>
        <AccountList />
      </Suspense>
      <h2>Connect an account</h2>
      <RoleForm label="Connect Gmail (read-only)" />
    </main>
  );
}
