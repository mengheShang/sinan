// Proposed changes waiting for the owner, shown above the digest.
// Server component: the list comes from the API, which expires and supersedes
// stale proposals before answering.
import { localTime } from "@/lib/format";
import { AssumedNote } from "../assumed";
import styles from "../digest.module.css";
import { DecideButtons } from "./decide-buttons";
import { linkBadge } from "./link";
import type { Due, Proposal } from "./types";

function dueText(due: Due, zone: string): string {
  if (due.due_at) {
    return localTime(due.due_at, zone);
  }
  if (due.due_raw) {
    return `“${due.due_raw}” (not parsed)`;
  }
  return "no due time";
}

function ProposalCard({ proposal, zone }: { proposal: Proposal; zone: string }) {
  const badge = linkBadge(proposal.link);
  return (
    <li className={styles.card}>
      <div className={styles.row}>
        {proposal.course ? <span className={styles.course}>{proposal.course}</span> : null}
        <strong>{proposal.title}</strong>
        {badge ? (
          <span className={styles.modelLink} role="note">
            {badge}
          </span>
        ) : null}
        <span className={styles.flag}>New due time proposed</span>
      </div>
      <p className={styles.due}>
        Due {dueText(proposal.before, zone)}
        <AssumedNote due={proposal.before} /> → <strong>{dueText(proposal.after, zone)}</strong>
        <AssumedNote due={proposal.after} />
      </p>
      {proposal.evidence ? (
        <blockquote className={styles.evidence}>{proposal.evidence}</blockquote>
      ) : null}
      <p className={styles.source}>
        {proposal.subject ? <span>{proposal.subject}</span> : null}
        {proposal.received_at ? <span>{localTime(proposal.received_at, zone)}</span> : null}
        {proposal.gmail_url ? (
          <a href={proposal.gmail_url} target="_blank" rel="noreferrer noopener">
            Open in Gmail
          </a>
        ) : null}
        <span>Expires {localTime(proposal.expires_at, zone)}</span>
      </p>
      <DecideButtons id={proposal.id} hash={proposal.payload_hash} />
    </li>
  );
}

export function ProposalList({
  proposals,
  zone,
}: {
  proposals: Proposal[] | "error";
  zone: string;
}) {
  if (proposals === "error") {
    return <p className={styles.bad}>Could not load proposed changes.</p>;
  }
  if (proposals.length === 0) {
    return null;
  }
  return (
    <section className={styles.section}>
      <h2>
        Proposed changes <span className={styles.count}>{proposals.length}</span>
      </h2>
      <p className={styles.meta}>
        An email changed an earlier deadline. Approve to update the item; nothing is sent
        anywhere.
      </p>
      <ul className={styles.list}>
        {proposals.map((p) => (
          <ProposalCard key={p.id} proposal={p} zone={zone} />
        ))}
      </ul>
    </section>
  );
}
