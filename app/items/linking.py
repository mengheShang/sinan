"""The link step: ask the model which listed item a change refers to (ADR 0019).

Extraction and the evaluation runner both use `ask`, so the evaluation
measures this exact step. The choices come from deterministic code
(`app.items.matching.choices`). The model only names a number, which is
checked against the list (`app.extraction.link_reply`). A refused reply, a
number outside the list, or a failed call for this change all mean no link.
A chosen item is only a target: the caller turns it into a pending proposal,
never into a change of the item.

No side effects of its own; `call` is the model call it is given.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.extraction.link_prompt import LINK_PROMPT, REPEAT_PROMPT, prepare_link
from app.extraction.link_reply import parse_target
from app.extraction.validate import ValidItem
from app.items.matching import Known
from app.providers.llm import ExtractorError

# Takes the link prompt and the prepared text, returns the raw reply
Call = Callable[[str, str], str]

# Not asked, because no item could be offered
NO_CHOICES = "no_choices"
# The call failed for this change only (an ExtractorError of scope "message")
FAILED = "error"


@dataclass(frozen=True)
class Pick[Key]:
    # The chosen item, or None for no link
    target: Key | None
    # How many choices the model was shown; 0 when it was not asked
    shown: int
    # "picked", "none", a refusal code from link_reply, NO_CHOICES or FAILED
    code: str


def ask[Key](
    call: Call,
    email: str,
    item: ValidItem,
    options: Sequence[Known[Key]],
    repeat: bool = False,
) -> Pick[Key]:
    """Ask once. `email` is the fenced email text from `prepare`. `repeat`
    asks REPEAT_PROMPT, whether a new item is an existing one (ADR 0023),
    instead of which item a change refers to.

    Raises ExtractorError of scope "service", like extraction, so the cycle
    stops and the email is read again later.
    """
    if not options:
        return Pick(None, 0, NO_CHOICES)
    shown = len(options)
    tag = "item" if repeat else "change"
    text = prepare_link(email, item, options, tag)
    prompt = REPEAT_PROMPT if repeat else LINK_PROMPT
    try:
        raw = call(prompt, text)
    except ExtractorError as err:
        if err.scope == "service":
            raise
        return Pick(None, shown, FAILED)
    found = parse_target(raw, shown)
    if found.number is None:
        return Pick(None, shown, found.code)
    chosen = options[found.number - 1]
    return Pick(chosen.key, shown, found.code)
