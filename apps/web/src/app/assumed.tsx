// Says which parts of a due time Sinan assumed rather than read (ADR 0016):
// the owner's zone when the email named none, 23:59 when it gave no time.
import styles from "./assumed.module.css";

export type Assumed = { zone_assumed: boolean; time_assumed: boolean };

export function assumedParts(due: Assumed): string[] {
  const parts: string[] = [];
  if (due.zone_assumed) {
    parts.push("zone assumed");
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
