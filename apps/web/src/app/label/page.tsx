// Labeling page (Issue #40): one email at a time, with a form for its labels.
// Saving writes the local label table only; nothing reaches Gmail and the
// model plays no part.
import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import { Suspense } from "react";
import { localTime } from "@/lib/format";
import { load, loadZone } from "../digest";
import styles from "./label.module.css";
import { skipEmail } from "./actions";
import { LabelForm } from "./label-form";
import type { Counts, Email, Queue } from "./types";

export const metadata: Metadata = { title: "Label · Sinan" };


function CountBar({ counts }: { counts: Counts }) {
  return (
    <p className={styles.counts}>
      <span>
        dev <strong>{counts.dev}</strong> of {counts.dev_total}
      </span>
      <span>
        test <strong>{counts.test}</strong> of {counts.test_total}
      </span>
      <span>
        skipped <strong>{counts.skipped}</strong>
      </span>
      <span>
        remaining <strong>{counts.remaining}</strong>
      </span>
    </p>
  );
}

function Nav({ email }: { email: Email }) {
  const link = (id: string | null, text: string) =>
    id ? <Link href={`/label?id=${id}`}>{text}</Link> : <span className={styles.muted}>{text}</span>;
  const nextOpen = email.next_open_id !== email.id ? email.next_open_id : null;
  return (
    <nav className={styles.row}>
      {link(email.prev_id, "← Previous")}
      {link(email.next_id, "Next →")}
      {link(nextOpen, "Next unlabeled")}
      {email.frozen ? null : (
        <form action={skipEmail}>
          <input type="hidden" name="id" value={email.id} />
          <button type="submit" disabled={email.skipped}>
            {email.skipped ? "Skipped" : "Skip"}
          </button>
        </form>
      )}
    </nav>
  );
}

function Status({ email }: { email: Email }) {
  const set = <strong>{email.eval_set}</strong>;
  if (email.frozen) {
    return (
      <p className={styles.note}>
        Labeled in the {set} set. The text below is the frozen snapshot; saving stores a
        correction and retires the old label.
      </p>
    );
  }
  if (email.skipped) {
    return <p className={styles.note}>In the {set} set. Skipped; you can still label it.</p>;
  }
  return <p className={styles.note}>In the {set} set. Not labeled yet.</p>;
}

function EmailView({ email, zone }: { email: Email; zone: string }) {
  return (
    <article className={styles.email}>
      <dl className={styles.meta}>
        <dt>From</dt>
        <dd>{email.from_addr ?? "–"}</dd>
        <dt>Subject</dt>
        <dd>{email.subject ?? "–"}</dd>
        <dt>Date</dt>
        <dd>
          {localTime(email.sent_at, zone)} <code>{email.sent_at}</code>
        </dd>
        <dt>Account</dt>
        <dd>{email.role}</dd>
      </dl>
      <pre className={styles.body}>{email.body_text ?? ""}</pre>
    </article>
  );
}

async function Labeler({ searchParams }: Pick<PageProps<"/label">, "searchParams">) {
  const { id } = await searchParams;
  if (typeof id !== "string" || id === "") {
    const queue = await load<Queue>("/labeling");
    if (queue === "error") {
      return <p className={styles.bad}>Could not load the labeling queue.</p>;
    }
    const start = queue.next_open_id ?? queue.first_id;
    if (start === null) {
      return (
        <p className={styles.muted}>
          No sampled emails yet. They appear once the owner draws the sets with{" "}
          <code>python -m apps.worker.sample</code>.
        </p>
      );
    }
    redirect(`/label?id=${start}`);
  }
  const [email, zone] = await Promise.all([
    load<Email>(`/labeling/emails/${encodeURIComponent(id)}`),
    loadZone(),
  ]);
  if (email === "error") {
    return (
      <p className={styles.bad}>
        Could not load this email. <Link href="/label">Start over</Link>
      </p>
    );
  }
  return (
    <>
      <CountBar counts={email.counts} />
      <Nav email={email} />
      <Status email={email} />
      <EmailView email={email} zone={zone} />
      {email.problems.length > 0 ? (
        <div className={styles.bad}>
          This email cannot be labeled as it is:
          <ul>
            {email.problems.map((p) => (
              <li key={p.field}>
                <code>{p.field}</code>: {p.message}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <LabelForm key={`${email.id}-${email.label_id ?? "new"}`} email={email} zone={zone} />
      )}
    </>
  );
}

export default function LabelPage({ searchParams }: PageProps<"/label">) {
  return (
    <main className={styles.main}>
      <p>
        <Link href="/">← Sinan</Link>
      </p>
      <h1>Label emails</h1>
      <p className={styles.note}>
        Labels go to the local database (ADR 0009). Only sampled emails are listed, dev set first,
        and each is labeled into the set its draw gave it. Nothing is sent anywhere.
      </p>
      <Suspense fallback={<p>Loading the email…</p>}>
        <Labeler searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
