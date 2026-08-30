"""
Shared bounded-string aliases for every user-supplied name/title/body field.

R3 — one source of truth per wire contract. Before this module every
``name``/``title`` in the package was a bare ``str``, so the edge accepted
anything the transport could carry::

    POST /api/repos/{id}/pipelines  {"name": "Q" * 60000}
      -> 201, persisted verbatim, and then rendered into `.card-header h3`
      -> measured card scrollWidth 66642px inside a 436px card, which pushes
         the Edit and Run buttons outside the clipped element

and the mirror-image hole::

    POST /api/repos  {"name": "   "}
      -> 201, an invisible, unlabelled row in the sidebar

Both are refused here, at the edge, with a 422 naming the field (R1) rather
than degrading into unusable UI.

WHERE THESE APPLY
-----------------
INPUT schemas only — ``*Create`` and ``*Update``. Deliberately NOT the
``*Read`` schemas, even where a ``*Base`` class is shared: rows written
before this bound exists must keep serializing. Bounding a response model
would turn yesterday's 60 000-character pipeline name from an ugly card into
a 500 on the list endpoint, which is strictly worse. Constrain what comes
in; keep reading what is already there.

CHOOSING THE BOUNDS
-------------------
``NAME_MAX = 200``. The longest name/title in the entire repo — tests,
fixtures, seed data and the ``.lazyaf/pipelines/*.yaml`` shipped with the
project — is 56 characters ("Remote lane: script step via the loopback
runner agent" and friends), so 200 clears real usage by more than 3x while
still fitting a card header on one or two lines. It is also the bound the QA
finding that opened this asked for.

``SENTENCE_MAX = 2000`` for an acceptance criterion: one sentence of prose,
not a name, but not a document either.

``BODY_MAX = 10000`` for descriptions, narratives and notes — free text that
legitimately runs to paragraphs. Generous enough that nobody hits it by
typing; tight enough that a 1 MB blob is still refused.

NOT bounded here: ``AgentFile.content`` and ``PromptTemplate.content``. Those
are whole file bodies (a markdown agent definition, a full prompt template)
and any bound short enough to be useful would be wrong for them.

NOT enforced here: control characters. A NUL byte in a name is a separate
finding with its own round-trip test asserting today's behaviour; changing
it belongs in that change, not this one.
"""

from typing import Annotated

from pydantic import StringConstraints

__all__ = ["NAME_MAX", "SENTENCE_MAX", "BODY_MAX", "Name", "Sentence", "Body"]

#: Max length of a short display name or title.
NAME_MAX = 200

#: Max length of a one-sentence field (an acceptance criterion).
SENTENCE_MAX = 2000

#: Max length of a free-text body (description, narrative, notes).
BODY_MAX = 10_000


#: A short display name or title. Stripped, non-blank, bounded.
#:
#: ``strip_whitespace`` runs BEFORE ``min_length``, so "   " and "\t\n " are
#: refused rather than stored as a blank row, and " My Pipeline " is stored
#: as "My Pipeline".
Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX),
]

#: A single sentence of user prose that must actually say something.
Sentence = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=SENTENCE_MAX),
]

#: Optional free-text body. May be empty; not stripped, because leading
#: whitespace can be meaningful in a markdown-ish description.
Body = Annotated[str, StringConstraints(max_length=BODY_MAX)]
