// How a proposal's item was chosen (ADR 0019). Pure, so it runs under
// `node --test` without a bundler.

export type Link = "deterministic" | "llm";

export const MODEL_LINK_BADGE = "Linked by the model, check it is the right item";

// The badge text for a proposal, or null when no badge is shown. Anything
// but "llm" (including a missing value) means the matcher chose the item.
export function linkBadge(link: Link | undefined): string | null {
  if (link === "llm") {
    return MODEL_LINK_BADGE;
  }
  return null;
}
