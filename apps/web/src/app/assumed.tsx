// Says which parts of a due time Sinan assumed rather than read (ADR 0016):
// the owner's zone when the email named none, 23:59 when it gave no time,
// and a date counted from the email's date ("in 3 days").
import styles from "./assumed.module.css";

export type Assumed = {
  zone_assumed: boolean;
  time_assumed: boolean;
  date_computed: boolean;
  time_carried: boolean;
  // The owner set the due time (ADR 0020); items only
  due_by_owner?: boolean;
};

export function assumedParts(due: Assumed): string[] {
  const parts: string[] = [];
  if (due.due_by_owner) {
    // Set by the owner, so nothing else about it was assumed
    return ["set by you"];
  }
  if (due.date_computed) {
    parts.push("computed from email date");
  }
  if (due.zone_assumed) {
    parts.push("zone assumed");
  }
  if (due.time_carried) {
    parts.push("time from earlier deadline");
  }
  if (due.time_assumed) {
    parts.push("time not stated");
  }
  return parts;
}

export function AssumedNote({ due }: { due: Assumed }) {
  const parts = assumedParts(due);
  if (parts.length === 0) {
    return null;
  }
  return <span className={styles.assumed}> ({parts.join(", ")})</span>;
}
