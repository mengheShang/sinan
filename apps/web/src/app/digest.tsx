// The daily digest and the account panel, shown on the home page.
// Server components only: the API is called with the session cookie, and a
// 401 sends the browser to the login page.
import Link from "next/link";
import { redirect } from "next/navigation";
import { localDate, localTime, reviewText } from "@/lib/format";
import { apiFetch } from "@/lib/session";
import styles from "./digest.module.css";
import { setStatus } from "./items/actions";

export type Item = {
  id: string;
  kind: "deadline" | "todo";
  course: string | null;
  title: string;
  due_raw: string | null;
  due_at: string | null;
  status: string;
  review: string | null;
  evidence: string | null;
  created_at: string;
  subject: string | null;
  sender: string | null;
  received_at: string | null;
  gmail_url: string | null;
};

type Digest = {
  digest_date: string;
  generated_at: string;
  since: string | null;
  timezone: string;
  new: Item[];
  due_soon: Item[];
  needs_review: Item[];
};

type Account = {
  id: string;
  email: string;
  role: string;
  expires_at: string | null;
  status: "ok" | "expiring" | "expired" | "needs_reauth";
  last_error: string | null;
  last_sync_at: string | null;
};

const STATUS_TEXT: Record<Account["status"], string> = {
  ok: "Connected",
  expiring: "Expires soon",
  expired: "Expired",
  needs_reauth: "Needs reconnect",
};

export async function load<T>(path: string): Promise<T | "error"> {
  const resp = await apiFetch(path);
  if (resp.status === 401) {
    redirect("/login");
  }
  if (!resp.ok) {
    return "error";
  }
  return (await resp.json()) as T;
}

const CLOSED = new Set(["done", "dismissed"]);

export function isClosed(item: Item): boolean {
  return CLOSED.has(item.status);
}

function StatusButton({ id, status, label }: { id: string; status: string; label: string }) {
  return (
    <form action={setStatus}>
      <input type="hidden" name="id" value={id} />
      <input type="hidden" name="status" value={status} />
      <button type="submit">{label}</button>
    </form>
  );
}

// Done and Dismiss on active items, Reopen on closed ones
function StatusButtons({ item }: { item: Item }) {
  if (isClosed(item)) {
    return (
      <div className={styles.actions}>
        <span className={styles.closed}>{item.status === "done" ? "Done" : "Dismissed"}</span>
        <StatusButton id={item.id} status="open" label="Reopen" />
      </div>
    );
  }
  return (
    <div className={styles.actions}>
      <StatusButton id={item.id} status="done" label="Done" />
      <StatusButton id={item.id} status="dismissed" label="Dismiss" />
    </div>
  );
}

export function ItemCard({ item, zone }: { item: Item; zone: string }) {
  const review = item.status === "needs_review";
  return (
    <li className={styles.card}>
      <div className={styles.row}>
        {item.course ? <span className={styles.course}>{item.course}</span> : null}
        <strong>{item.title}</strong>
        {review ? <span className={styles.flag}>{reviewText(item.review)}</span> : null}
      </div>
      <p className={styles.due}>
        {item.due_at ? (
          <>Due {localTime(item.due_at, zone)}</>
        ) : item.due_raw ? (
          <>Due “{item.due_raw}” (not parsed)</>
        ) : (
          <>No due time</>
        )}
      </p>
      {item.evidence ? <blockquote className={styles.evidence}>{item.evidence}</blockquote> : null}
      <p className={styles.source}>
        {item.subject ? <span>{item.subject}</span> : null}
        {item.received_at ? <span>{localTime(item.received_at, zone)}</span> : null}
        {item.gmail_url ? (
          <a href={item.gmail_url} target="_blank" rel="noreferrer noopener">
            Open in Gmail
          </a>
        ) : null}
      </p>
      <StatusButtons item={item} />
    </li>
  );
}

export function Section({
  title,
  empty,
  items,
  zone,
}: {
  title: string;
  empty: string;
  items: Item[];
  zone: string;
}) {
  return (
    <section className={styles.section}>
      <h2>
        {title} <span className={styles.count}>{items.length}</span>
      </h2>
      {items.length === 0 ? (
        <p className={styles.empty}>{empty}</p>
      ) : (
        <ul className={styles.list}>
          {items.map((item) => (
            <ItemCard key={item.id} item={item} zone={zone} />
          ))}
        </ul>
      )}
    </section>
  );
}

function DigestView({ digest }: { digest: Digest | null | "error" }) {
  if (digest === "error") {
    return <p className={styles.bad}>Could not load the digest.</p>;
  }
  if (digest === null) {
    return (
      <p className={styles.empty}>
        No digest yet. The Worker makes one each morning after mail has synced.
      </p>
    );
  }
  const zone = digest.timezone;
  // Items closed since the digest was made drop out at once, not next morning
  const all = [...digest.due_soon, ...digest.needs_review, ...digest.new];
  const closed = new Set(all.filter(isClosed).map((i) => i.id));
  const active = (items: Item[]) => items.filter((i) => !closed.has(i.id));
  return (
    <>
      <p className={styles.meta}>
        {localDate(digest.digest_date)} · made {localTime(digest.generated_at, zone)}
        {closed.size > 0 ? (
          <>
            {" "}
            · {closed.size} closed since then, <Link href="/items">see items</Link>
          </>
        ) : null}
      </p>
      <Section
        title="Due in the next 7 days"
        empty="Nothing due this week."
        items={active(digest.due_soon)}
        zone={zone}
      />
      <Section
        title="Needs review"
        empty="Nothing to check."
        items={active(digest.needs_review)}
        zone={zone}
      />
      <Section
        title={digest.since ? "New since the last digest" : "New"}
        empty="No new items."
        items={active(digest.new)}
        zone={zone}
      />
    </>
  );
}

function AccountPanel({ accounts, zone }: { accounts: Account[] | "error"; zone: string }) {
  if (accounts === "error") {
    return <p className={styles.bad}>Could not load accounts.</p>;
  }
  if (accounts.length === 0) {
    return (
      <p className={styles.empty}>
        No accounts yet. <Link href="/accounts">Connect Gmail</Link>.
      </p>
    );
  }
  const warn = accounts.some((a) => a.status !== "ok");
  return (
    <section className={styles.panel}>
      {warn ? (
        <p role="status" className={styles.bad}>
          An account needs attention. <Link href="/accounts">Reconnect it</Link> so mail keeps
          syncing.
        </p>
      ) : null}
      <ul className={styles.accounts}>
        {accounts.map((a) => (
          <li key={a.id}>
            <strong>{a.email}</strong>
            <span className={styles.course}>{a.role}</span>
            <span className={styles[a.status]}>{STATUS_TEXT[a.status]}</span>
            <span className={styles.muted}>
              Last sync {a.last_sync_at ? localTime(a.last_sync_at, zone) : "not yet"}
            </span>
            <span className={styles.muted}>
              Token expires {localTime(a.expires_at, zone)}
            </span>
            {a.last_error ? <code className={styles.muted}>{a.last_error}</code> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

// Loads both in parallel; account times use the digest zone, or UTC before the first digest
export async function Dashboard() {
  const [digest, accounts] = await Promise.all([
    load<Digest | null>("/digest"),
    load<Account[]>("/accounts"),
  ]);
  const zone = digest && digest !== "error" ? digest.timezone : "UTC";
  return (
    <>
      <AccountPanel accounts={accounts} zone={zone} />
      <DigestView digest={digest} />
    </>
  );
}
