"""Labeling endpoints for the Dashboard (Issue #40, ADR 0009 decision 3).

All sit behind the login like every other route. Writes go to the local
database only: nothing reaches Gmail and the model plays no part.

Only emails of the current sampling draw (Issue #41) are in the queue, and
each is labeled into the set its draw gave it; the request does not choose.
Any other email answers 404.

Request bodies are read as plain JSON and checked here, so a 422 lists
field paths and messages only. FastAPI's own 422 would echo the submitted
values, which can quote the email.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.db import labeling
from app.db.label_format import Labels, Ref
from app.db.labeling import Invalid, Problem
from apps.api.oauth import SessionDep

router = APIRouter(prefix="/labeling")

MAX_TAGS = 20


class LabelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labels: Labels
    tags: list[Ref] = Field(default=[], max_length=MAX_TAGS)


class CorrectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labels: Labels
    tags: list[Ref] = Field(default=[], max_length=MAX_TAGS)


class CountsOut(BaseModel):
    # Labeled emails in each set's current draw, and the size of that draw
    dev: int
    test: int
    dev_total: int
    test_total: int
    skipped: int
    remaining: int


class QueueOut(BaseModel):
    counts: CountsOut
    # The oldest email neither labeled nor skipped, or null when none is left
    next_open_id: uuid.UUID | None
    # The first sampled email, so the page can open one when nothing is left
    first_id: uuid.UUID | None


class ProblemOut(BaseModel):
    field: str
    message: str


class RefOut(BaseModel):
    ref: str
    title: str
    eval_set: str


class EmailOut(BaseModel):
    id: uuid.UUID
    from_addr: str | None
    subject: str | None
    body_text: str | None
    sent_at: datetime
    role: str
    frozen: bool
    label_id: uuid.UUID | None
    eval_set: str | None
    labels: dict[str, Any] | None
    tags: list[str]
    skipped: bool
    problems: list[ProblemOut]
    prev_id: uuid.UUID | None
    next_id: uuid.UUID | None
    next_open_id: uuid.UUID | None
    refs: list[RefOut]
    counts: CountsOut


class SavedOut(BaseModel):
    label_id: uuid.UUID | None
    next_open_id: uuid.UUID | None
    counts: CountsOut


def counts_out(session: SessionDep) -> CountsOut:
    found = labeling.counts(session)
    return CountsOut(
        dev=found.dev,
        test=found.test,
        dev_total=found.dev_total,
        test_total=found.test_total,
        skipped=found.skipped,
        remaining=found.remaining,
    )


def invalid(problems: list[Problem]) -> JSONResponse:
    detail = [{"field": p.field, "message": p.message} for p in problems]
    return JSONResponse({"detail": detail}, status.HTTP_422_UNPROCESSABLE_CONTENT)


def parse[M: BaseModel](model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise Invalid(labeling.problems_of(err, "")) from None


@router.get("")
def queue(session: SessionDep) -> QueueOut:
    """Counts per set and where to start."""
    return QueueOut(
        counts=counts_out(session),
        next_open_id=labeling.next_open(session),
        first_id=labeling.first_id(session),
    )


@router.get("/emails/{message_id}")
def email(message_id: uuid.UUID, session: SessionDep) -> EmailOut:
    """One email with its current label, if any, and its neighbours."""
    try:
        view = labeling.read_email(session, message_id)
    except labeling.UnknownEmail as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a sampled email") from exc
    data = vars(view) | {"counts": counts_out(session)}
    data["problems"] = [vars(p) for p in view.problems]
    return EmailOut.model_validate(data)


@router.post("/emails/{message_id}/label", response_model=SavedOut)
def save(
    message_id: uuid.UUID,
    session: SessionDep,
    data: Annotated[Any, Body()] = None,
) -> SavedOut | JSONResponse:
    """Freeze the email and store its label in one transaction."""
    try:
        body = parse(LabelBody, data)
        label_id = labeling.save(session, message_id, body.labels, body.tags)
    except Invalid as exc:
        session.rollback()
        return invalid(exc.problems)
    except labeling.UnknownEmail as exc:
        session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a sampled email") from exc
    except labeling.Conflict as exc:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Already labeled; correct it instead"
        ) from exc
    session.commit()
    return SavedOut(
        label_id=label_id,
        next_open_id=labeling.next_open(session, message_id),
        counts=counts_out(session),
    )


@router.post("/labels/{label_id}/correct", response_model=SavedOut)
def correct(
    label_id: uuid.UUID,
    session: SessionDep,
    data: Annotated[Any, Body()] = None,
) -> SavedOut | JSONResponse:
    """Retire a label and store its correction in one transaction."""
    try:
        body = parse(CorrectBody, data)
        new_id = labeling.correct(session, label_id, body.labels, body.tags)
    except Invalid as exc:
        session.rollback()
        return invalid(exc.problems)
    except labeling.UnknownLabel as exc:
        session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown label") from exc
    except labeling.Conflict as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Label was already corrected") from exc
    session.commit()
    return SavedOut(label_id=new_id, next_open_id=None, counts=counts_out(session))


@router.post("/emails/{message_id}/skip")
def skip(message_id: uuid.UUID, session: SessionDep) -> SavedOut:
    """Leave an email out of the queue without labeling it."""
    try:
        labeling.skip(session, message_id)
    except labeling.UnknownEmail as exc:
        session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a sampled email") from exc
    except labeling.Conflict as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Already labeled") from exc
    session.commit()
    return SavedOut(
        label_id=None,
        next_open_id=labeling.next_open(session, message_id),
        counts=counts_out(session),
    )
