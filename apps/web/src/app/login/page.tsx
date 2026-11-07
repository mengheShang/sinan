import type { Metadata } from "next";
import { LoginForm } from "./login-form";
import styles from "./login.module.css";

export const metadata: Metadata = { title: "Sign in · Sinan" };

export default function LoginPage() {
  return (
    <main className={styles.main}>
      <h1>Sinan</h1>
      <p>Sign in to see your dashboard.</p>
      <LoginForm />
    </main>
  );
}
