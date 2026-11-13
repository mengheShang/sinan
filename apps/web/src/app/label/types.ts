// Shapes of the labeling API (apps/api/labeling.py) and of the form.
// The label fields follow evals/README.md.

// dev and test count labeled emails of the current draw; the totals are its size
export type Counts = {
  dev: number;
  test: number;
  dev_total: number;
  test_total: number;
  skipped: number;
  remaining: number;
};

export type Problem = { field: string; message: string };

export type ItemLabel = {
  ref: string;
  kind: "deadline" | "todo";
  course: string | null;
  title: string;
  due_raw: string | null;
  due_at: string | null;
  due_ambiguous: boolean;
  changes: string | null;
};

export type Labels = { items: ItemLabel[]; safe_to_auto_read: boolean };

export type Email = {
  id: string;
  from_addr: string | null;
  subject: string | null;
  body_text: string | null;
  sent_at: string;
  role: string;
  frozen: boolean;
  label_id: string | null;
  eval_set: "dev" | "test" | null;
  labels: Labels | null;
  tags: string[];
  skipped: boolean;
  problems: Problem[];
  prev_id: string | null;
  next_id: string | null;
  next_open_id: string | null;
  refs: { ref: string; title: string; eval_set: string }[];
  counts: Counts;
};

export type Queue = { counts: Counts; next_open_id: string | null; first_id: string | null };

// What the save action sends back to the form; field paths and messages only
export type SaveState = { problems: Problem[]; error: string | null };
