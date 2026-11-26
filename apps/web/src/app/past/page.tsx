import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import { type Item, load, loadZone, type NoticeGroup, NoticeSection, Section } from "../digest";
import styles from "../page.module.css";

export const metadata: Metadata = { title: "Past items · Sinan" };

type Past = { past: Item[]; stale: Item[]; notice_groups: NoticeGroup[] };
// Open and review items that have left the home page; their status is unchanged
async function PastItems() {
  const [past, zone] = await Promise.all([load<Past>("/items/past"), loadZone()]);
  if (past === "error") {
    return <p>Could not load items.</p>;
  }
  return (
    <>
      <Section title="Past due" empty="Nothing past due." items={past.past} zone={zone} />
      <Section title="Stale, needs review" empty="No stale items." items={past.stale} zone={zone} />
      <NoticeSection title="Notices" empty="No notices." groups={past.notice_groups} zone={zone} />
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
        time whose email is over 14 days old. They left the home page but keep their status:
        mark them done or dismiss them here. Open notices, grouped by category and newest first,
        as on the home page; make one a to-do if it needs your action, or dismiss it to clear it.
      </p>
      <Suspense fallback={<p>Loading items…</p>}>
        <PastItems />
      </Suspense>
    </main>
  );
}
