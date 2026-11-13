"use client";

// The label form: every field of evals/README.md. It builds the JSON the API
// expects; the API checks it with the Pydantic models and answers with field
// paths and messages only, which are shown next to the fields.
import { useActionState, useState } from "react";
import { localTime } from "@/lib/format";
import { saveLabel } from "./actions";
import styles from "./label.module.css";
import type { Email, ItemLabel, Problem, SaveState } from "./types";

type Draft = {
  ref: string;
  kind: "deadline" | "todo";
  course: string;
  title: string;
  due_raw: string;
  due_at: string;
  due_ambiguous: boolean;
  changes: string;
};

const EMPTY: Draft = {
  ref: "",
  kind: "deadline",
  course: "",
  title: "",
  due_raw: "",
  due_at: "",
  due_ambiguous: false,
  changes: "",
};

const initial: SaveState = { problems: [], error: null };

function draftOf(item: ItemLabel): Draft {
  return {
    ref: item.ref,
    kind: item.kind,
    course: item.course ?? "",
    title: item.title,
    due_raw: item.due_raw ?? "",
    due_at: item.due_at ?? "",
    due_ambiguous: item.due_ambiguous,
    changes: item.changes ?? "",
  };
}

// Empty text means null; the API decides whether that is allowed
function orNull(value: string): string | null {
  const text = value.trim();
  return text ? text : null;
}

function itemOf(draft: Draft): ItemLabel {
  return {
    ref: draft.ref.trim(),
    kind: draft.kind,
    course: orNull(draft.course),
    title: draft.title.trim(),
    due_raw: orNull(draft.due_raw),
    due_at: orNull(draft.due_at),
    due_ambiguous: draft.due_ambiguous,
    changes: orNull(draft.changes),
  };
}

function splitTags(text: string): string[] {
  return text
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
}

// Problems for one field path, or for anything under it
function under(problems: Problem[], path: string): Problem[] {
  return problems.filter((p) => p.field === path || p.field.startsWith(`${path}.`));
}

function Errors({ problems }: { problems: Problem[] }) {
  if (problems.length === 0) {
    return null;
  }
  return (
    <ul className={styles.errors}>
      {problems.map((p, n) => (
        <li key={`${p.field}-${n}`}>
          <code>{p.field}</code>: {p.message}
        </li>
      ))}
    </ul>
  );
}

function DueHint({ value, zone }: { value: string; zone: string }) {
  const text = value.trim();
  if (!text.endsWith("Z") || Number.isNaN(Date.parse(text))) {
    return null;
  }
  return <span className={styles.hint}>= {localTime(text, zone)}</span>;
}

type ItemProps = {
  n: number;
  draft: Draft;
  zone: string;
  refsId: string;
  problems: Problem[];
  onChange: (next: Draft) => void;
  onRemove: () => void;
};

function ItemFields({ n, draft, zone, refsId, problems, onChange, onRemove }: ItemProps) {
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => onChange({ ...draft, [key]: value });
  const id = (name: string) => `item-${n}-${name}`;
  return (
    <fieldset className={styles.item}>
      <legend>Item {n + 1}</legend>
      <div className={styles.grid}>
        <label htmlFor={id("ref")}>ref</label>
        <input
          id={id("ref")}
          value={draft.ref}
          placeholder="cs000-hw3"
          onChange={(e) => set("ref", e.target.value)}
        />
        <label htmlFor={id("kind")}>kind</label>
        <select
          id={id("kind")}
          value={draft.kind}
          onChange={(e) => set("kind", e.target.value as Draft["kind"])}
        >
          <option value="deadline">deadline</option>
          <option value="todo">todo</option>
        </select>
        <label htmlFor={id("course")}>course</label>
        <input
          id={id("course")}
          value={draft.course}
          placeholder="empty for none"
          onChange={(e) => set("course", e.target.value)}
        />
        <label htmlFor={id("title")}>title</label>
        <input id={id("title")} value={draft.title} onChange={(e) => set("title", e.target.value)} />
        <label htmlFor={id("due_raw")}>due_raw</label>
        <input
          id={id("due_raw")}
          value={draft.due_raw}
          placeholder="as written in the email; empty for none"
          onChange={(e) => set("due_raw", e.target.value)}
        />
        <label htmlFor={id("due_at")}>due_at</label>
        <span className={styles.stack}>
          <input
            id={id("due_at")}
            value={draft.due_at}
            placeholder="2026-11-14T07:59:00Z (UTC); empty if ambiguous"
            onChange={(e) => set("due_at", e.target.value)}
          />
          <DueHint value={draft.due_at} zone={zone} />
        </span>
        <label htmlFor={id("due_ambiguous")}>due_ambiguous</label>
        <input
          id={id("due_ambiguous")}
          type="checkbox"
          checked={draft.due_ambiguous}
          onChange={(e) => set("due_ambiguous", e.target.checked)}
        />
        <label htmlFor={id("changes")}>changes</label>
        <input
          id={id("changes")}
          value={draft.changes}
          list={refsId}
          placeholder="ref of an earlier item; empty for a new item"
          onChange={(e) => set("changes", e.target.value)}
        />
      </div>
      <Errors problems={problems} />
      <button type="button" onClick={onRemove}>
        Remove item
      </button>
    </fieldset>
  );
}

export function LabelForm({ email, zone }: { email: Email; zone: string }) {
  const [state, action, pending] = useActionState(saveLabel, initial);
  const stored = email.labels;
  const [drafts, setDrafts] = useState<Draft[]>(stored ? stored.items.map(draftOf) : []);
  const [safe, setSafe] = useState(stored ? stored.safe_to_auto_read : false);
  const [tags, setTags] = useState(email.tags.join(", "));
  const [evalSet, setEvalSet] = useState<"dev" | "test">(email.eval_set ?? "dev");
  const correcting = email.label_id !== null;
  const body: Record<string, unknown> = {
    labels: { items: drafts.map(itemOf), safe_to_auto_read: safe },
    tags: splitTags(tags),
  };
  if (!correcting) {
    body.eval_set = evalSet;
  }
  const payload = JSON.stringify(body);
  const refsId = `refs-${email.id}`;
  const problems = state.problems;
  const change = (n: number, next: Draft) => setDrafts(drafts.map((d, i) => (i === n ? next : d)));
  const remove = (n: number) => setDrafts(drafts.filter((_, i) => i !== n));
  const known = new Set(["eval_set", "tags", "labels.safe_to_auto_read"]);
  const general = problems.filter(
    (p) => !p.field.startsWith("labels.items.") && !known.has(p.field),
  );
  return (
    <form action={action} className={styles.form}>
      <input type="hidden" name="id" value={email.id} />
      <input type="hidden" name="label_id" value={email.label_id ?? ""} />
      <input type="hidden" name="payload" value={payload} />
      <datalist id={refsId}>
        {email.refs.map((r) => (
          <option key={r.ref} value={r.ref}>
            {r.title} ({r.eval_set})
          </option>
        ))}
      </datalist>
      <div className={styles.row}>
        <label htmlFor="eval_set">Set</label>
        {correcting ? (
          <strong id="eval_set">{email.eval_set}</strong>
        ) : (
          <select
            id="eval_set"
            value={evalSet}
            onChange={(e) => setEvalSet(e.target.value as "dev" | "test")}
          >
            <option value="dev">dev</option>
            <option value="test">test</option>
          </select>
        )}
      </div>
      <Errors problems={under(problems, "eval_set")} />
      <h2>Items {drafts.length === 0 ? <span className={styles.muted}>none</span> : null}</h2>
      {drafts.map((d, n) => (
        <ItemFields
          key={n}
          n={n}
          draft={d}
          zone={zone}
          refsId={refsId}
          problems={under(problems, `labels.items.${n}`)}
          onChange={(next) => change(n, next)}
          onRemove={() => remove(n)}
        />
      ))}
      <Errors problems={problems.filter((p) => p.field === "labels.items")} />
      <div className={styles.row}>
        <button type="button" onClick={() => setDrafts([...drafts, EMPTY])}>
          Add item
        </button>
      </div>
      <div className={styles.row}>
        <input
          id="safe_to_auto_read"
          type="checkbox"
          checked={safe}
          onChange={(e) => setSafe(e.target.checked)}
        />
        <label htmlFor="safe_to_auto_read">safe_to_auto_read</label>
      </div>
      <Errors problems={under(problems, "labels.safe_to_auto_read")} />
      <div className={styles.row}>
        <label htmlFor="tags">tags</label>
        <input
          id="tags"
          value={tags}
          placeholder="course, extension"
          onChange={(e) => setTags(e.target.value)}
        />
      </div>
      <Errors problems={under(problems, "tags")} />
      <Errors problems={general} />
      <div className={styles.row}>
        <button type="submit" disabled={pending}>
          {pending ? "Saving…" : correcting ? "Save correction" : "Save label"}
        </button>
        <p aria-live="polite" className={styles.bad}>
          {state.error}
        </p>
      </div>
    </form>
  );
}
