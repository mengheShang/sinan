import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import { type Item, load, Section } from "../digest";
import styles from "../page.module.css";

export const metadata: Metadata = { title: "Items · Sinan" };

type Zone = { timezone: string } | null;

// Closed items, so a mistaken Done or Dismiss can be undone with Reopen
async function ClosedItems() {
  const [done, dismissed, digest] = await Promise.all([
    load<Item[]>("/items?status=done"),
    load<Item[]>("/items?status=dismissed"),
    load<Zone>("/digest"),
  ]);
  if (done === "error" || dismissed === "error") {
    return <p>Could not load items.</p>;
  }
  // Times use the digest zone, or UTC before the first digest
  const zone = digest && digest !== "error" ? digest.timezone : "UTC";
  return (
    <>
      <Section title="Done" empty="Nothing marked done." items={done} zone={zone} />
      <Section title="Dismissed" empty="Nothing dismissed." items={dismissed} zone={zone} />
    </>
  );
}

export default function ItemsPage() {
  return (
    <main className={styles.main}>
      <p>
        <Link href="/">← Sinan</Link>
      </p>
      <h1>Closed items</h1>
      <p className={styles.note}>
        Items you marked done or dismissed. They stay out of new digests until reopened.
      </p>
      <Suspense fallback={<p>Loading items…</p>}>
        <ClosedItems />
      </Suspense>
    </main>
  );
}
