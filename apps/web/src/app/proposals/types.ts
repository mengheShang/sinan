// Shapes shared by the proposal list, its buttons and their server actions

export type Due = {
  due_raw: string | null;
  due_at: string | null;
  zone_assumed: boolean;
  time_assumed: boolean;
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
