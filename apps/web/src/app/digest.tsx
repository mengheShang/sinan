// The live home page (ADR 0022) and the account panel, plus the item cards
// and notice groups the other pages share.
// Server components only: the API is called with the session cookie, and a
// 401 sends the browser to the login page.
import Link from "next/link";
import { redirect } from "next/navigation";
import { localParts } from "@/lib/due";
import { localTime, reviewText } from "@/lib/format";
import { AssumedNote } from "./assumed";
import { apiFetch } from "@/lib/session";
import styles from "./digest.module.css";
import { dismissNotices, makeTodo, setStatus } from "./items/actions";
import { ConfirmButton, DueForm, type Suggestion } from "./items/due-form";
import { ProposalList } from "./proposals/list";
import type { Proposal } from "./proposals/types";

export type Item = {
  id: string;
  kind: "deadline" | "todo" | "notice";
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
  zone_assumed: boolean;
  time_assumed: boolean;
  date_computed: boolean;
  time_carried: boolean;
  // The notice's group (ADR 0020); "other" for an item without one
  category: string;
  // Home page tags (ADR 0022); false on every other page
  overdue?: boolean;
  added_today?: boolean;
  // The model's suggested due time, for an item without one (ADR 0022)
  suggestion?: Suggestion | null;
};

// One category's notices, in the fixed category order (ADR 0020)
export type NoticeGroup = {
  category: string;
  notices: Item[];
};

type Home = {
  timezone: string;
  // Items with a due time, overdue first, then those needing review last
  due: Item[];
  // Open items without a due time, newest first
  todo: Item[];
  // Every open notice, newest first, grouped by category (ADR 0017, 0020)
  notice_groups: NoticeGroup[];
  // Proposed changes that expired unanswered in the last 7 days
  expired_proposals: number;
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

// The zone times are shown in (SINAN_TIMEZONE); UTC if the API cannot say
export async function loadZone(): Promise<string> {
  const found = await load<{ timezone: string }>("/timezone");
  return found === "error" ? "UTC" : found.timezone;
}

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

// Add due, Edit due and Confirm on active deadlines and to-dos (ADR 0022).
// Confirm on an item with no due time opens the due form instead. The key
// remounts the form once the due time changes, so it closes after a save.
function DueControls({ item, zone }: { item: Item; zone: string }) {
  if (isClosed(item) || item.kind === "notice") {
    return null;
  }
  const review = item.status === "needs_review";
  // Without a due time, the model's suggestion prefills the form
  const suggestion = item.due_at ? null : (item.suggestion ?? null);
  const parts = localParts(item.due_at ?? suggestion?.due_at ?? null, zone);
  const key = item.due_at ?? "none";
  if (review && !item.due_at) {
    return (
      <div className={styles.actions}>
        <DueForm
          key={key}
          id={item.id}
          label="Confirm"
          {...parts}
          zone={zone}
          suggestion={suggestion}
        />
      </div>
    );
  }
  const label = item.due_at ? "Edit due" : "Add due";
  return (
    <div className={styles.actions}>
      {review ? <ConfirmButton id={item.id} /> : null}
      <DueForm
        key={key}
        id={item.id}
        label={label}
        {...parts}
        zone={zone}
        suggestion={suggestion}
      />
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
        {item.overdue ? <span className={styles.overdue}>Overdue</span> : null}
        {item.added_today ? <span className={styles.added}>Added today</span> : null}
        {review ? <span className={styles.flag}>{reviewText(item.review, item.due_raw)}</span> : null}
      </div>
      <p className={styles.due}>
        {item.due_at ? (
          <>
            Due {localTime(item.due_at, zone)}
            <AssumedNote due={item} />
          </>
        ) : item.due_raw ? (
          <>Due “{item.due_raw}” (not parsed)</>
        ) : (
          <>No due time</>
        )}
      </p>
      <DueControls item={item} zone={zone} />
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

// One line per notice: title, due date if any, link to the email, the fix for
// a wrong demotion, and Dismiss. A notice the owner made a to-do says so
// instead; a dismissed one reads "Dismissed" and can be reopened.
function NoticeLine({ item, zone }: { item: Item; zone: string }) {
  const notice = item.kind === "notice";
  return (
    <li className={styles.notice}>
      {item.course ? <span className={styles.course}>{item.course}</span> : null}
      <strong>{item.title}</strong>
      {item.added_today ? <span className={styles.added}>Added today</span> : null}
      {item.due_at ? (
        <span>
          {localTime(item.due_at, zone)}
          <AssumedNote due={item} />
        </span>
      ) : item.due_raw ? (
        <span>“{item.due_raw}”</span>
      ) : null}
      {item.gmail_url ? (
        <a href={item.gmail_url} target="_blank" rel="noreferrer noopener">
          Open in Gmail
        </a>
      ) : item.subject ? (
        <span className={styles.muted}>{item.subject}</span>
      ) : null}
      {notice && !isClosed(item) ? (
        <div className={styles.actions}>
          <form action={makeTodo}>
            <input type="hidden" name="id" value={item.id} />
            <button type="submit">Make it a to-do</button>
          </form>
          <StatusButton id={item.id} status="dismissed" label="Dismiss" />
        </div>
      ) : notice ? (
        <StatusButtons item={item} />
      ) : (
        <>
          <span className={styles.closed}>Now a to-do</span>
          <StatusButtons item={item} />
        </>
      )}
    </li>
  );
}

// Dismisses exactly the open notices it is given: those shown in one group, or
// in the whole section; one arriving after the page loaded stays
function DismissAll({ items, label = "Dismiss all" }: { items: Item[]; label?: string }) {
  const open = items.filter((i) => i.kind === "notice" && !isClosed(i));
  if (open.length === 0) {
    return null;
  }
  return (
    <form action={dismissNotices} className={styles.actions}>
      {open.map((i) => (
        <input key={i.id} type="hidden" name="id" value={i.id} />
      ))}
      <button type="submit">{label}</button>
    </form>
  );
}

const CATEGORY_TEXT: Record<string, string> = {
  course: "Courses",
  safety: "Safety",
  event: "Events",
  health: "Health",
  career: "Career",
  orders: "Orders",
  other: "Other",
};

// One row per category: name, count and the short titles; open it for the
// notice lines, each with its own actions, and the group's own Dismiss all
function NoticeGroupRow({ group, zone }: { group: NoticeGroup; zone: string }) {
  const name = CATEGORY_TEXT[group.category] ?? CATEGORY_TEXT.other;
  const titles = group.notices.map((i) => i.title).join(" · ");
  return (
    <details className={styles.group}>
      <summary>
        <strong>{name}</strong> <span className={styles.count}>{group.notices.length}</span>{" "}
        <span className={styles.titles}>{titles}</span>
      </summary>
      <ul className={styles.notices}>
        {group.notices.map((item) => (
          <NoticeLine key={item.id} item={item} zone={zone} />
        ))}
      </ul>
      <DismissAll items={group.notices} label={`Dismiss all ${name.toLowerCase()}`} />
    </details>
  );
}

export function NoticeSection({
  title,
  empty,
  groups,
  zone,
}: {
  title: string;
  empty: string;
  groups: NoticeGroup[];
  zone: string;
}) {
  const shown = groups.flatMap((g) => g.notices);
  const total = shown.length;
  return (
    <section className={styles.section}>
      <h2>
        {title} <span className={styles.count}>{total}</span>
      </h2>
      {total === 0 ? (
        <p className={styles.empty}>{empty}</p>
      ) : (
        <>
          <div className={styles.groups}>
            {groups.map((group) => (
              <NoticeGroupRow key={group.category} group={group} zone={zone} />
            ))}
          </div>
          {/* Every notice shown in the section, across all groups (ADR 0017, decision 7) */}
          <DismissAll items={shown} />
        </>
      )}
    </section>
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

function HomeView({ home }: { home: Home | "error" }) {
  if (home === "error") {
    return <p className={styles.bad}>Could not load the home page.</p>;
  }
  const zone = home.timezone;
  const expired = home.expired_proposals;
  return (
    <>
      <p className={styles.meta}>
        {expired > 0 ? (
          <>
            {expired} proposed {expired === 1 ? "change" : "changes"} expired unanswered this
            week ·{" "}
          </>
        ) : null}
        Items past due over two days are on the <Link href="/past">Past items</Link> page
      </p>
      <Section title="Due" empty="Nothing due." items={home.due} zone={zone} />
      <Section title="To-do" empty="Nothing to do." items={home.todo} zone={zone} />
      <NoticeSection title="Notices" empty="No notices." groups={home.notice_groups} zone={zone} />
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

// Loads all three in parallel; account times use the home page's zone
export async function Dashboard() {
  const [home, accounts, proposals] = await Promise.all([
    load<Home>("/home"),
    load<Account[]>("/accounts"),
    load<Proposal[]>("/proposals"),
  ]);
  const zone = home === "error" ? "UTC" : home.timezone;
  return (
    <>
      <AccountPanel accounts={accounts} zone={zone} />
      <ProposalList proposals={proposals} zone={zone} />
      <HomeView home={home} />
    </>
  );
}
