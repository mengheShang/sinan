"use client";

import { useActionState } from "react";
import { login, type LoginState } from "./actions";
import styles from "./login.module.css";

const initial: LoginState = { error: null, username: "" };

export function LoginForm() {
  const [state, action, pending] = useActionState(login, initial);
  return (
    <form action={action} className={styles.form}>
      <label htmlFor="username">Username</label>
      <input
        id="username"
        name="username"
        type="text"
        autoComplete="username"
        autoCapitalize="none"
        spellCheck={false}
        defaultValue={state.username}
        required
        autoFocus
      />
      <label htmlFor="password">Password</label>
      <input
        id="password"
        name="password"
        type="password"
        autoComplete="current-password"
        required
      />
      <button type="submit" disabled={pending}>
        {pending ? "Signing in…" : "Sign in"}
      </button>
      <p aria-live="polite" className={styles.error}>
        {state.error}
      </p>
    </form>
  );
}
