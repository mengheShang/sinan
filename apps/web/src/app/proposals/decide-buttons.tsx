"use client";

// Approve and Reject for one proposed change. The id and hash come from props
// on every render, so a reset after the action cannot change what is sent.
import { useActionState } from "react";
import styles from "../digest.module.css";
import { decide } from "./actions";
import type { DecideState } from "./types";

const START: DecideState = { message: "" };

export function DecideButtons({ id, hash }: { id: string; hash: string }) {
  const [state, action, pending] = useActionState(decide, START);
  const done = state.message !== "";
  return (
    <div className={styles.actions}>
      <form action={action}>
        <input type="hidden" name="id" value={id} readOnly />
        <input type="hidden" name="decision" value="approve" readOnly />
        <input type="hidden" name="payload_hash" value={hash} readOnly />
        <button type="submit" disabled={pending || done}>
          Approve
        </button>
      </form>
      <form action={action}>
        <input type="hidden" name="id" value={id} readOnly />
        <input type="hidden" name="decision" value="reject" readOnly />
        <button type="submit" disabled={pending || done}>
          Reject
        </button>
      </form>
      {done ? (
        <p role="status" className={styles.bad}>
          {state.message}
        </p>
      ) : null}
    </div>
  );
}
