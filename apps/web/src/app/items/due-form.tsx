"use client";

// The due form and the Confirm button (ADR 0022, decision 3). The form takes a
// local date and an optional time in the owner's zone; with no time the API
// uses 23:59 and the item says so.
import { useActionState } from "react";
import styles from "../digest.module.css";
import type { DueState } from "@/lib/due";
import { confirmItem, saveDue } from "./due-actions";

const START: DueState = { message: "" };

// The model's suggested due time (ADR 0022, decision 4); it only prefills
export type Suggestion = {
  id: string;
  due_at: string;
  time_assumed: boolean;
  quote: string;
};

type FormProps = {
  id: string;
  // The button text: "Add due", "Edit" or "Confirm"
  label: string;
  // Prefilled local date and time, or empty strings
  date: string;
  time: string;
  zone: string;
  // Shown and sent with the save when the form was prefilled from it
  suggestion?: Suggestion | null;
};

export function DueForm({ id, label, date, time, zone, suggestion }: FormProps) {
  const [state, action, pending] = useActionState(saveDue, START);
  return (
    <details className={styles.dueForm}>
      <summary>{label}</summary>
      {suggestion ? (
        <p className={styles.suggested}>
          Suggested by the model from the email, not applied until you save:{" "}
          <q>{suggestion.quote}</q>
        </p>
      ) : null}
      <form action={action}>
        <input type="hidden" name="id" value={id} readOnly />
        {suggestion ? (
          <input type="hidden" name="suggestion_id" value={suggestion.id} readOnly />
        ) : null}
        <label>
          Date <input type="date" name="date" defaultValue={date} required />
        </label>
        <label>
          Time <input type="time" name="time" defaultValue={time} />
        </label>
        <span className={styles.muted}>{zone}; no time means 11:59 PM</span>
        <button type="submit" disabled={pending}>
          Save
        </button>
      </form>
      {state.message ? (
        <p role="status" className={styles.bad}>
          {state.message}
        </p>
      ) : null}
    </details>
  );
}

export function ConfirmButton({ id }: { id: string }) {
  const [state, action, pending] = useActionState(confirmItem, START);
  return (
    <form action={action}>
      <input type="hidden" name="id" value={id} readOnly />
      <button type="submit" disabled={pending}>
        Confirm
      </button>
      {state.message ? (
        <p role="status" className={styles.bad}>
          {state.message}
        </p>
      ) : null}
    </form>
  );
}
