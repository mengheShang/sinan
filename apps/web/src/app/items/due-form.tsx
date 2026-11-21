"use client";

// The due form and the Confirm button (ADR 0022, decision 3). The form takes a
// local date and an optional time in the owner's zone; with no time the API
// uses 23:59 and the item says so.
import { useActionState } from "react";
import styles from "../digest.module.css";
import type { DueState } from "@/lib/due";
import { confirmItem, saveDue } from "./due-actions";

const START: DueState = { message: "" };

type FormProps = {
  id: string;
  // The button text: "Add due", "Edit" or "Confirm"
  label: string;
  // Prefilled local date and time, or empty strings
  date: string;
  time: string;
  zone: string;
};

export function DueForm({ id, label, date, time, zone }: FormProps) {
  const [state, action, pending] = useActionState(saveDue, START);
  return (
    <details className={styles.dueForm}>
      <summary>{label}</summary>
      <form action={action}>
        <input type="hidden" name="id" value={id} readOnly />
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
