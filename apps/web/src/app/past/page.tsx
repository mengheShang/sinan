import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import { type Item, load, NoticeSection, Section } from "../digest";
import styles from "../page.module.css";

export const metadata: Metadata = { title: "Past items · Sinan" };

type Past = { past: Item[]; stale: Item[]; notices: Item[] };
type Zone = { timezone: string } | null;

// Open and review items that have left the digest; their status is unchanged
async function PastItems() {
  const [past, digest] = await Promise.all([load<Past>("/items/past"), load<Zone>("/digest")]);
  if (past === "error") {
    return <p>Could not load items.</p>;
  }
  // Times use the digest zone, or UTC before the first digest
  const zone = digest && digest !== "error" ? digest.timezone : "UTC";
  return (
    <>
      <Section title="Past due" empty="Nothing past due." items={past.past} zone={zone} />
      <Section title="Stale, needs review" empty="No stale items." items={past.stale} zone={zone} />
      <NoticeSection
        title="Notices"
        empty="No notices."
        items={past.notices}
        zone={zone}
      />
    </>
  );
}

export default function PastPage() {
  return (
    <main className={styles.main}>
      <p>
        <Link href="/">← Sinan</Link>
      </p>
      <h1>Past items</h1>
      <p className={styles.note}>
        Items whose due time passed more than two days ago, and items needing review with no due
        time whose email is over 14 days old. They left the digest but keep their status: mark
        them done or dismiss them here. Open notices, newest first, each shown once in a morning
        digest; make one a to-do if it needs your action, or dismiss it to clear it from here.
      </p>
      <Suspense fallback={<p>Loading items…</p>}>
        <PastItems />
      </Suspense>
    </main>
  );
}
