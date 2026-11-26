// Shapes shared by the proposal list, its buttons and their server actions
import type { Link } from "./link";

export type Due = {
  due_raw: string | null;
  due_at: string | null;
  zone_assumed: boolean;
  time_assumed: boolean;
  date_computed: boolean;
  time_carried: boolean;
};

export type Proposal = {
  id: string;
  kind: "item_update";
  status: string;
  item_id: string;
  course: string | null;
  title: string;
  before: Due;
  after: Due;
  evidence: string | null;
  // How the item was chosen; part of the hashed payload (ADR 0019)
  link: Link;
  // Made from an email read as a new item that matched this one (ADR 0023);
  // Reject saves that item separately
  from_new_item: boolean;
  payload_hash: string;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  subject: string | null;
  sender: string | null;
  received_at: string | null;
  gmail_url: string | null;
};

export type DecideState = { message: string };
