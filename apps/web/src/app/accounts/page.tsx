import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import { Suspense } from "react";
import { apiFetch } from "@/lib/session";
import { authorize, check, connect } from "./actions";
import styles from "./accounts.module.css";

export const metadata: Metadata = { title: "Accounts · Sinan" };

type Status = "ok" | "expiring" | "expired" | "needs_reauth";

// A write or calendar token's state (ADR 0021); never the token
type Grant = {
  authorized_at: string;
  expires_at: string;
  status: Status;
  last_error: string | null;
};

type Account = {
  id: string;
  email: string;
  role: "school" | "personal";
  authorized_at: string | null;
  expires_at: string | null;
  status: Status;
  last_error: string | null;
  last_sync_at: string | null;
  write: Grant | null;
  calendar: Grant | null;
  // The Executor's public key is set, so the two flows can run
  can_enable: boolean;
};

const MESSAGES: Record<string, string> = {
  connected: "Account connected with read-only access.",
  denied: "You declined access on Google's page. Nothing was stored.",
  scope: "Google did not grant exactly read-only Gmail access, so nothing was stored.",
  expired: "That connect request expired or was already used. Try again.",
  error: "Connecting failed. Check the API logs and try again.",
  checked: "Token checked.",
  actions_enabled: "Actions enabled. The token is sealed so that only the Executor can use it.",
  calendar_connected:
    "Calendar connected. The token is sealed so that only the Executor can use it.",
  wrong_account:
    "Google signed in a different account than the one you chose, so nothing was stored.",
  not_set_up: "Actions are off until SINAN_EXECUTOR_PUBLIC_KEY is set in your .env.",
  check_failed: "Could not reach Google to check the token. Try again later.",
};

const STATUS_TEXT: Record<Status, string> = {
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

const GOOD = new Set(["connected", "checked", "actions_enabled", "calendar_connected"]);

async function Banner({ searchParams }: { searchParams: Promise<Record<string, unknown>> }) {
  const { oauth } = await searchParams;
  const text = typeof oauth === "string" ? MESSAGES[oauth] : undefined;
  if (!text) {
    return null;
  }
  const good = GOOD.has(String(oauth));
  return (
    <p role="status" className={good ? styles.good : styles.bad}>
      {text}
    </p>
  );
}

const GRANT_NAME = { write: "Actions", calendar: "Calendar" } as const;
const GRANT_BUTTON = { write: "Enable actions", calendar: "Connect calendar" } as const;
const GRANT_SCOPE = {
  write: "Gmail modify: mark read, labels, archive. Never send or delete.",
  calendar: "Calendar events only, and your address to check the account.",
} as const;

// One row per write or calendar token: its state, and the button that
// starts (or renews) its flow for this account
function GrantRow({ account, purpose }: { account: Account; purpose: "write" | "calendar" }) {
  const grant = account[purpose];
  const label = grant ? `Reconnect ${GRANT_NAME[purpose].toLowerCase()}` : GRANT_BUTTON[purpose];
  return (
    <div className={styles.row}>
      <strong>{GRANT_NAME[purpose]}</strong>
      {grant ? (
        <>
          <span className={styles[grant.status]}>{STATUS_TEXT[grant.status]}</span>
          <span>Expires {utc(grant.expires_at)}</span>
          {grant.last_error ? <code>{grant.last_error}</code> : null}
        </>
      ) : (
        <span>Off</span>
      )}
      {account.can_enable ? (
        <form action={authorize}>
          <input type="hidden" name="id" value={account.id} />
          <input type="hidden" name="purpose" value={purpose} />
          <button type="submit" title={GRANT_SCOPE[purpose]}>
            {label}
          </button>
        </form>
      ) : null}
    </div>
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
            <dt>Last sync</dt>
            <dd>{a.last_sync_at ? utc(a.last_sync_at) : "Not yet"}</dd>
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
          <GrantRow account={a} purpose="write" />
          <GrantRow account={a} purpose="calendar" />
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
      <p>
        Actions and the calendar are separate grants, each for one account and each sealed so that
        only the Executor can use it. They expire after 7 days too.
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
