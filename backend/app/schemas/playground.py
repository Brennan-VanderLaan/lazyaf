"""
Playground schemas for ephemeral agent testing.
"""

import base64
import binascii
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas._datetime import UTCDateTime

# An agent prompt is a prompt, not a payload. 64 KiB is roughly ten thousand
# words - far past any real task description and far short of the 280 KB blob
# the QA probe posted whole with no complaint. Refusing at the edge (R1) beats
# discovering it inside a container that already cost money to start.
MAX_TASK_LENGTH = 64 * 1024


class PlaygroundTestRequest(BaseModel):
    """Request to start a playground test."""

    agent_id: str | None = None  # Platform agent file ID
    repo_agent_name: str | None = None  # OR repo-defined agent name
    # The vocabulary is NOT re-spelled here. `agent_run.AGENT_BY_RUNNER_TYPE`
    # is the one source of truth for which runner types exist (R3); this
    # field validates against it so the frontend and the backend cannot drift
    # into two different vocabularies again, and an unknown value is a loud
    # 422 rather than a silent fallback to claude-code.
    runner_type: str = "claude-code"
    model: str | None = None  # Specific model (e.g., claude-sonnet-4-20250514, gemini-2.5-pro)
    branch: str  # Branch to test against
    task_override: str | None = Field(default=None, max_length=MAX_TASK_LENGTH)
    save_to_branch: str | None = None  # If set, save changes to this branch
    # Files a human attached to this prompt. DECLARED even though every
    # non-empty value is currently refused (`attachment_refusal`), because
    # pydantic ignores unknown keys: without this field a client that posted
    # `attachments` would get a 200 and have its images silently dropped, which
    # is worse than the loud 422 it gets now. See the Attachments section at
    # the bottom of this module for what has to land before it can be honoured.
    #
    # Forward-referenced: `PlaygroundAttachment` is defined below, next to the
    # limits and the sniffer it belongs with, and `model_rebuild()` at the foot
    # of the module resolves it.
    attachments: list["PlaygroundAttachment"] = Field(default_factory=list)

    @field_validator("attachments")
    @classmethod
    def _bounded_attachment_count(cls, value: list) -> list:
        """Count cap, enforced at call time rather than as `max_length`.

        `Field(max_length=MAX_ATTACHMENTS)` would evaluate the constant while
        this class body runs, and the constant lives at the bottom of the
        module with the rest of the attachment machinery. A validator resolves
        it when the request arrives - and gets to say the number out loud,
        which `max_length`'s stock message does not.
        """
        if len(value) > MAX_ATTACHMENTS:
            raise ValueError(
                f"{len(value)} attachments; the playground accepts at most "
                f"{MAX_ATTACHMENTS}"
            )
        return value

    @field_validator("runner_type")
    @classmethod
    def _known_runner_type(cls, value: str) -> str:
        # Imported lazily: the schema layer must not carry a service import at
        # module scope just to know a vocabulary.
        from app.services.agent_run import AGENT_BY_RUNNER_TYPE

        if value not in AGENT_BY_RUNNER_TYPE:
            known = ", ".join(sorted(AGENT_BY_RUNNER_TYPE))
            raise ValueError(
                f"unknown runner_type {value!r}; known runner types are {known}"
            )
        return value

    @field_validator("task_override")
    @classmethod
    def _task_not_blank(cls, value: str | None) -> str | None:
        """A whitespace-only prompt must not start an agent container.

        The button is guarded client-side too, but the button is not the
        contract - this is.
        """
        if value is None:
            return None
        if value.strip() == "":
            raise ValueError("task_override cannot be blank")
        return value


class PlaygroundTestResponse(BaseModel):
    """Response from starting a playground test."""

    session_id: str
    status: str  # "queued" | "running"
    message: str


class PlaygroundStatus(BaseModel):
    """Current status of a playground session."""

    session_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    # Where these facts came from. "session" is the live in-memory session;
    # "run" is the durable PipelineRun the session left behind, read after the
    # 30-minute in-memory TTL swept it (or after a backend restart).
    source: Literal["session", "run"] = "session"


class PlaygroundResult(BaseModel):
    """Result of a completed playground test."""

    session_id: str
    status: str
    diff: str | None = None  # Git diff output
    files_changed: list[str] = []
    branch_saved: str | None = None  # Branch name if saved
    error: str | None = None
    logs: str = ""
    duration_seconds: float | None = None
    # See PlaygroundStatus.source. This is load-bearing, not decoration: a
    # playground work branch is DELETED once its diff has been computed
    # (`agent_run._dispose_playground_branch`), so a result read from the
    # durable run record has the full transcript but CANNOT have the diff.
    # Reporting `diff: null` without saying why would be indistinguishable
    # from "the agent changed nothing", which is the silent-loss shape R1
    # forbids - the client renders a different sentence for source="run".
    source: Literal["session", "run"] = "session"


class PlaygroundSessionSummary(BaseModel):
    """One past playground run, read from its durable PipelineRun.

    12.5 already leaves a complete record of every playground run: a
    PipelineRun with ``trigger_type='playground'`` and
    ``trigger_ref=<session_id>``, whose single StepRun carries the transcript,
    hanging off a hidden ``__lazyaf_adhoc__:playground:<id>`` Pipeline that
    carries the prompt. History is a READ of that, not a new table.
    """

    session_id: str
    run_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    prompt: str
    agent: str | None = None
    model: str | None = None
    base_branch: str | None = None
    work_branch: str | None = None
    created_at: UTCDateTime
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    duration_seconds: float | None = None
    # True while the in-memory session still exists, which is the only window
    # in which this run's DIFF can still be shown (see PlaygroundResult.source).
    live: bool = False


class PlaygroundLogEvent(BaseModel):
    """SSE event for log streaming."""

    type: str  # "log" | "tool" | "status" | "complete" | "error" | "ping"
    data: str
    timestamp: UTCDateTime


# =============================================================================
# Attachments - the playground's modality half (Milestone 14.5)
# =============================================================================
#
# WHAT THIS SECTION IS FOR. The Endpoints page answers "can this model see an
# image"; the playground is where a human would act on that answer. This is the
# wire shape for acting on it, and the edge validation that shape needs.
#
# WHAT IT DOES NOT DO YET, STATED HERE BECAUSE THE ALTERNATIVE IS A LIE.
# An attachment posted here is REFUSED (see `attachment_refusal`), because
# there is currently no path from this field to the model's prompt. The field
# exists anyway, and declaring it is strictly safer than leaving it out:
# pydantic IGNORES unknown keys, so a client posting `attachments` at a schema
# without this field would get a 200 and have its images silently dropped -
# exactly the invisible downgrade R1 forbids. A loud 422 naming the missing
# plumbing is the honest answer until the plumbing exists.


#: Image media types the playground accepts. Kept small on purpose: these four
#: are what every OpenAI-compatible vision stack we target actually decodes,
#: and a format accepted here but rejected upstream costs a container start to
#: discover.
ALLOWED_IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")

#: Per-file cap. 5 MiB is far past any screenshot and far short of a payload
#: that would blow the request body.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

#: Whole-request cap. NOT `MAX_ATTACHMENTS * MAX_ATTACHMENT_BYTES`: four 5 MiB
#: images is a ~27 MB base64 body, and the thing being protected here is the
#: request, not the file.
MAX_ATTACHMENTS_TOTAL_BYTES = 8 * 1024 * 1024

#: How many. Four is a contact sheet; past that the prompt is a photo album and
#: the context window is the real limit anyway.
MAX_ATTACHMENTS = 4

#: base64 inflates by 4/3 plus padding. Checking the ENCODED length first means
#: a 200 MB string is refused without ever being decoded into memory.
_MAX_ENCODED_CHARS = ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4 + 64


def sniff_image_media_type(data: bytes) -> str | None:
    """The media type the BYTES say, or None when they say nothing we know.

    The client's declared `media_type` is never trusted on its own. A filename
    and a content type are both things a client asserts; the first bytes of the
    file are what a vision encoder actually has to decode. A mislabelled upload
    refused here produces a fixable sentence ("you declared image/png and these
    bytes are a JPEG"); the same file accepted here produces an opaque 400 from
    the inference server, inside a container that already cost money to start.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class PlaygroundAttachment(BaseModel):
    """One file a human attached to a playground prompt.

    Transported as base64 in the JSON body. That is ugly and deliberate: it
    needs no new route, no new storage, no new secret and no multipart parser,
    and the caps above keep the body bounded. If attachments ever outgrow this,
    the replacement is an upload route - not a bigger cap.
    """

    kind: Literal["image"] = "image"
    #: What the client SAYS this is. Validated against the bytes below, and
    #: never believed on its own.
    media_type: str
    #: Display only. Shown back to the human so two screenshots can be told
    #: apart; never used to decide the type, and never used as a path.
    filename: str = Field(default="", max_length=255)
    data_base64: str
    #: Decoded length, DERIVED by validation rather than client-supplied: a
    #: claimed size is one more thing that can disagree with the payload.
    size_bytes: int = 0

    @field_validator("media_type")
    @classmethod
    def _known_media_type(cls, value: str) -> str:
        if value not in ALLOWED_IMAGE_MEDIA_TYPES:
            allowed = ", ".join(ALLOWED_IMAGE_MEDIA_TYPES)
            raise ValueError(
                f"unsupported attachment media type {value!r}; "
                f"the playground accepts {allowed}"
            )
        return value

    @model_validator(mode="after")
    def _decode_and_prove(self) -> "PlaygroundAttachment":
        name = self.filename or "(unnamed)"
        encoded = self.data_base64 or ""
        if not encoded.strip():
            raise ValueError(f"attachment {name} carries no data")
        if len(encoded) > _MAX_ENCODED_CHARS:
            # Refused WITHOUT decoding: the point of checking the encoded
            # length first is not to materialise a hostile payload in memory.
            raise ValueError(
                f"attachment {name} is larger than the "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB per-file limit"
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"attachment {name} is not valid base64: {exc}") from exc
        if not raw:
            raise ValueError(f"attachment {name} decoded to zero bytes")
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment {name} is {len(raw)} bytes, over the "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB per-file limit"
            )
        sniffed = sniff_image_media_type(raw)
        if sniffed is None:
            raise ValueError(
                f"attachment {name} is not a PNG, JPEG, WebP or GIF; its "
                f"leading bytes match no format the playground can send"
            )
        if sniffed != self.media_type:
            # R1: quietly rewriting media_type to the sniffed value would be a
            # silent fix for a client that is confused about its own data.
            raise ValueError(
                f"attachment {name} declares {self.media_type} but its bytes "
                f"are {sniffed}"
            )
        self.size_bytes = len(raw)
        return self


#: Can an attachment actually REACH the model today? NO - and saying so out
#: loud is the whole of this constant's job.
#:
#: The chain from this field to a prompt is: routers/playground ->
#: playground_service.start_test -> agent_run.start_adhoc_agent_run (step
#: config) -> pipeline_executor._attach_agent_payload, which builds
#: `exec_config["agent"]` from a hand-picked field list -> the agent config
#: file -> runner_common.agent_wrapper -> harness.executor -> Transcript.
#: The LAST link decides it: `harness.transcript.Entry.content` is typed `str`,
#: `Entry.chars` measures it with `len()`, and the token estimator, the elision
#: policy and the fallback parser all assume that. An image content part is a
#: LIST of parts, so today it has nowhere to go.
#:
#: Until those change, accepting a file here would store bytes the model never
#: sees and the run would SUCCEED - a right-looking answer produced from a
#: prompt that silently lost half its input. That is worse than the
#: `supports_tools` case R1 was written for, because nothing fails.
#:
#: FLIPPING THIS IS ONE LINE. The files that must land first are named in
#: ATTACHMENT_DELIVERY_GAP so nobody has to re-derive them.
ATTACHMENTS_REACH_THE_MODEL = False

ATTACHMENT_DELIVERY_GAP = (
    "LazyAF cannot yet deliver an attachment to a model. The harness "
    "transcript types every message's content as a string "
    "(runner_common.harness.transcript.Entry.content), so an image content "
    "part has nowhere to go - an accepted file would stop at the database "
    "while the run reported success. The path that has to land first is "
    "pipeline_executor._attach_agent_payload, runner_common.agent_config, "
    "runner_common.harness.executor and runner_common.harness.transcript."
)


class PlaygroundAttachmentLimits(BaseModel):
    """The caps, on the wire.

    The UI RENDERS these rather than re-spelling them. R3: a "max 5 MiB" in a
    Svelte template plus a `5 * 1024 * 1024` in a validator is two sources of
    truth for one contract, and the half that drifts is always the sentence.
    """

    max_files: int = MAX_ATTACHMENTS
    max_bytes_per_file: int = MAX_ATTACHMENT_BYTES
    max_bytes_total: int = MAX_ATTACHMENTS_TOTAL_BYTES
    media_types: list[str] = list(ALLOWED_IMAGE_MEDIA_TYPES)


class PlaygroundModalitySupport(BaseModel):
    """Whether the PLATFORM can carry one modality, and why not when it cannot.

    Deliberately NOT about any particular endpoint. An endpoint's answer is a
    probe result and lives on `EndpointCapabilities.modalities`; this is the
    platform-wide fact that sits in front of it. BOTH have to be true before a
    human can attach anything, and collapsing them would make "your endpoint
    cannot see" and "LazyAF cannot send" the same sentence when they call for
    opposite actions - probe the endpoint, versus wait for the plumbing.
    """

    modality: str
    #: True only when a human could attach this RIGHT NOW.
    attachable: bool
    #: Populated in BOTH states. A control greyed for a reason nobody wrote
    #: down is the thing this whole section exists to avoid.
    reason: str


class PlaygroundCapabilities(BaseModel):
    """What the playground itself can carry, for the UI to render."""

    attachment_limits: PlaygroundAttachmentLimits = PlaygroundAttachmentLimits()
    modalities: list[PlaygroundModalitySupport]


def playground_capabilities() -> PlaygroundCapabilities:
    """The one producer of the projection above (R3)."""
    return PlaygroundCapabilities(
        modalities=[
            PlaygroundModalitySupport(
                modality="images",
                attachable=ATTACHMENTS_REACH_THE_MODEL,
                reason=(
                    "Attach images to an endpoint that has demonstrated it "
                    "accepts an image content part."
                    if ATTACHMENTS_REACH_THE_MODEL
                    else ATTACHMENT_DELIVERY_GAP
                ),
            ),
            PlaygroundModalitySupport(
                modality="audio",
                attachable=False,
                reason=(
                    "Audio DETECTION is honest; audio INPUT is deliberately "
                    "not offered. Nothing in the harness consumes an audio "
                    "reply, and a Whisper-class encoder pads to a fixed 30s "
                    "window - one millisecond of silence can be billed as "
                    "~1500 prompt tokens. The chip tells you what the endpoint "
                    "can do; this control stays shut until there is a loop "
                    "that can use it."
                ),
            ),
            PlaygroundModalitySupport(
                modality="video",
                attachable=False,
                reason=(
                    "The OpenAI chat-completions wire format has no video "
                    "content part - the vocabulary is text, image_url, "
                    "input_audio and file. LazyAF cannot send video to any "
                    "endpoint, whatever the model can do. vLLM's `video_url` "
                    "is a documented OpenAI-incompatible extension LazyAF does "
                    "not speak, and frame-sampling into N image parts is "
                    "images wearing a hat."
                ),
            ),
        ]
    )


def attachment_refusal(attachments: list["PlaygroundAttachment"]) -> str | None:
    """Why this request's attachments must be refused, or None to proceed.

    Returns None for an EMPTY list, ALWAYS. A modality refusal is conditional
    on the request actually attaching something: refusing every
    attachment-less playground run because the harness cannot carry an image
    would be a self-inflicted outage dressed as rigour. It is the same
    asymmetry that keeps `supports_images` out of the unconditional dispatch
    refusal - almost every step sends no image.

    Deliberately does NOT consult the endpoint's `supports_images`. That
    question has exactly one owner - `endpoint_modality_refusal` in
    services/model_endpoints/resolve.py, called at dispatch - and a second
    implementation here would be a second thing to keep in step (R3). While
    the delivery gap stands, the platform-wide refusal is both true and
    strictly stronger, so consulting the endpoint would change no outcome
    while adding a second answer to one question.
    """
    if not attachments:
        return None
    if not ATTACHMENTS_REACH_THE_MODEL:
        return ATTACHMENT_DELIVERY_GAP
    total = sum(int(a.size_bytes or 0) for a in attachments)
    if total > MAX_ATTACHMENTS_TOTAL_BYTES:
        return (
            f"attachments total {total} bytes, over the "
            f"{MAX_ATTACHMENTS_TOTAL_BYTES // (1024 * 1024)} MiB "
            f"whole-request limit"
        )
    return None
# `PlaygroundTestRequest.attachments` is annotated with a forward reference so
# the attachment machinery can live next to the limits and the sniffer it
# belongs with. This resolves it.
PlaygroundTestRequest.model_rebuild()
