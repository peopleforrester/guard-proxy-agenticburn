# ABOUTME: Conformance for the folded code-of-conduct term list (#81, #84) on /chat, the reply mask and the wall,
# ABOUTME: the masked prompt on a refused turn's spans, and the optional Bedrock guardrail on the prompt, stubbed.
from __future__ import annotations

import codecs
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

from fakes import FakeUpstream, Recorded, Reply, a2a_result

pytestmark = pytest.mark.guards

REFUSAL = "Let's keep it kind: BurritoBot only answers within the event's code of conduct."
CAPTURE = {"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_ONLY"}
FIXTURES = Path(__file__).resolve().parent / "coc_floor"
# The /chat asks the stage sends; every other group of stage texts is what a reply can carry.
STAGE_PROMPT_GROUPS = ("kubecon-prompt", "replay-prompt", "catalog-prompt", "probe-prompt")


def _abuse_set() -> list[tuple[str, str, str]]:
    """(category, text, the mask the default list gives it), decoded: the spike's floor.py mask for its 144
    entries, and #84's slang on top."""
    out = []
    for line in (FIXTURES / "abuse-set.rot13.tsv").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            cat, text, masked = line.split("\t")
            out.append((cat, codecs.decode(text, "rot13"), codecs.decode(masked, "rot13")))
    return out


ABUSE = _abuse_set()
STAGE = json.loads((FIXTURES / "stage-texts.json").read_text(encoding="utf-8"))["texts"]


def _forwarded_prompts(agent) -> list[str]:
    return [c.json()["params"]["message"]["parts"][0]["text"] for c in agent.calls()]


def test_the_fixtures_are_the_spikes_measurement():
    # The spike's floor caught 111 of its 155 abuse texts, every evasion among them; #84's slang adds one
    # Portuguese text. The stage texts are every page's, as the spike measured them.
    assert len(ABUSE) == 155
    caught = [cat for cat, text, masked in ABUSE if masked != text]
    assert len(caught) == 112
    assert caught.count("evasion") == sum(cat == "evasion" for cat, _, _ in ABUSE) == 25
    assert sum(len(v) for v in STAGE.values()) > 4000


# --- The abuse set ------------------------------------------------------------------------------------


def test_the_abuse_set_is_refused_exactly_where_the_default_list_matches(start_proxy, agent):
    p = start_proxy()
    wrong = []
    for cat, text, masked in ABUSE:
        refused = p.chat(text).json()["reply"] == REFUSAL
        if refused != (masked != text):
            wrong.append((cat, refused))
    assert wrong == []
    # The 43 texts the terms miss reach the agent exactly as typed; no refused one does. Compared as a
    # boolean, so a failure prints no abuse text.
    passed = [text for _, text, masked in ABUSE if masked == text]
    same = _forwarded_prompts(agent) == passed
    assert same, "the agent got other prompts than the 43 the terms pass"
    assert p.events().count("conduct_refused") == 112


def test_every_evasion_is_refused(start_proxy, agent):
    p = start_proxy()
    for _, text, _ in [a for a in ABUSE if a[0] == "evasion"]:
        assert p.chat(text).json()["reply"] == REFUSAL
    assert agent.calls() == []


def test_a_reply_is_masked_as_the_fixture_masks_it(start_proxy, agent):
    agent.respond = lambda req: Reply(
        body=a2a_result(
            ABUSE[int(req.json()["params"]["message"]["parts"][0]["text"].split()[1])][1]
        )
    )
    p = start_proxy()
    wrong = [
        (i, cat)
        for i, (cat, _, masked) in enumerate(ABUSE)
        if p.chat(f"reply {i}").json()["reply"] != masked
    ]
    assert wrong == []
    assert p.events().count("conduct_masked") == 112


def test_the_wall_masks_as_the_fixture_masks_it(start_proxy, agent):
    # No block-list, so the wall's mask is the terms' alone; no abuse text carries personal data.
    p = start_proxy(STREAM_PROMPTS="on", BLOCK_LIST="")
    for start in range(0, len(ABUSE), 50):
        batch = ABUSE[start : start + 50]
        for _, text, _ in batch:
            p.chat(text)
        shown = p.get("/prompts").json()["prompts"][-len(batch) :]
        # By index, so a failure prints no abuse text.
        assert [start + i for i, (_, _, m) in enumerate(batch) if shown[i] != m] == []


# --- The stage's own texts ----------------------------------------------------------------------------


def test_every_stage_prompt_reaches_the_agent(start_proxy, agent):
    prompts = [t for g in STAGE_PROMPT_GROUPS for t in STAGE[g]]
    p = start_proxy()
    refused = [t for t in prompts if p.chat(t).json()["reply"] == REFUSAL]
    assert refused == []
    assert _forwarded_prompts(agent) == prompts
    assert "conduct_refused" not in p.events()


@pytest.mark.parametrize("group", sorted(set(STAGE) - set(STAGE_PROMPT_GROUPS)))
def test_no_stage_text_is_masked_in_a_reply(start_proxy, agent, group):
    # One reply per group. "#" is no letter, stand-in or separator, so no match can span two texts.
    reply = "\n#\n".join(STAGE[group])
    agent.respond = lambda req: Reply(body=a2a_result(reply))
    p = start_proxy()
    assert p.chat("one burrito").json()["reply"] == reply
    assert "conduct_masked" not in p.events()


# --- The spans of a refused turn ----------------------------------------------------------------------


def _envelope(role: str, text: str) -> str:
    if role == "user":
        return json.dumps([{"role": "user", "parts": [{"type": "text", "content": text}]}])
    return json.dumps(
        [
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": text}],
                "finish_reason": "stop",
            }
        ]
    )


def _turn_spans(collector) -> dict[str, dict]:
    def found(spans):
        got = {s["name"]: s for s in spans if s["name"] in ("POST /chat", "chat")}
        return got if len(got) == 2 else None

    return collector.wait_for(found, "the POST /chat and chat spans")


@pytest.mark.spans
def test_a_refused_prompt_s_chat_span_carries_it_masked(traced, agent):
    p, collector = traced(**CAPTURE)
    assert p.chat("one burrito, you f.u.c.k.i.n.g b1tch").json()["reply"] == REFUSAL
    spans = _turn_spans(collector)
    for name in ("POST /chat", "chat"):
        attrs = spans[name]["attributes"]
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.input.messages"] == _envelope(
            "user", "one burrito, you [redacted] [redacted]"
        )
        assert attrs["gen_ai.output.messages"] == _envelope("assistant", REFUSAL)
    assert spans["chat"]["parent_span_id"] == spans["POST /chat"]["span_id"]
    # No span anywhere carries the prompt as typed, and the agent was never called.
    assert all("b1tch" not in json.dumps(s) for s in collector.spans())
    assert "agent.forward" not in {s["name"] for s in collector.spans()}
    assert agent.calls() == []


@pytest.mark.spans
def test_a_refused_prompt_s_spans_carry_no_content_without_capture(traced, agent):
    p, collector = traced()
    assert p.chat("one burrito, you fuuuck").json()["reply"] == REFUSAL
    spans = _turn_spans(collector)
    for s in spans.values():
        assert s["attributes"]["gen_ai.operation.name"] == "chat"
        assert "gen_ai.input.messages" not in s["attributes"]
        assert "gen_ai.output.messages" not in s["attributes"]


# --- The Bedrock guardrail, stubbed -------------------------------------------------------------------

GUARDRAIL_ID = "gr-conduct01"
GUARDRAIL_VERSION = "3"
REGION = "us-west-2"
KEY_ID = "AKIDCONFORMANCE"
SECRET = "conformance-secret-key"
SESSION_TOKEN = "conformance-session-token"
APPLY_PATH = f"/guardrail/{GUARDRAIL_ID}/version/{GUARDRAIL_VERSION}/apply"
INSULT = "BurritoBot, you are a clown who cannot code"


def _verdict(action: str) -> dict:
    return {
        "action": action,
        "outputs": [{"text": "Keep it kind."}] if action != "NONE" else [],
        "assessments": [],
        "usage": {"contentPolicyUnits": 1},
    }


@pytest.fixture
def bedrock() -> Iterator[FakeUpstream]:
    """A Bedrock runtime stub: ApplyGuardrail answers that the guardrail did not intervene."""
    up = FakeUpstream(lambda req: Reply(body=_verdict("NONE")))
    yield up
    up.close()


@pytest.fixture
def guarded_proxy(start_proxy, bedrock, tmp_path):
    """Start the implementation with a guardrail configured, its AWS endpoint the stub and its credentials
    static. No shared AWS file and no instance metadata, so nothing of the host's own AWS setup is read."""

    def start(**env: str):
        full = {
            "CONDUCT_GUARDRAIL_ID": GUARDRAIL_ID,
            "CONDUCT_GUARDRAIL_VERSION": GUARDRAIL_VERSION,
            "AWS_REGION": REGION,
            "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": bedrock.url,
            "AWS_ACCESS_KEY_ID": KEY_ID,
            "AWS_SECRET_ACCESS_KEY": SECRET,
            "AWS_SESSION_TOKEN": SESSION_TOKEN,
            "AWS_CONFIG_FILE": str(tmp_path / "no-aws-config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-aws-credentials"),
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        full.update(env)
        return start_proxy(**{k: v for k, v in full.items() if v is not None})

    return start


def _sigv4_signature(req: Recorded, secret: str) -> tuple[str, str]:
    """The signature AWS would compute for req, and the one req carries, from its own signed headers."""
    auth = req.headers.get("Authorization") or req.headers["authorization"]
    algorithm, rest = auth.split(" ", 1)
    assert algorithm == "AWS4-HMAC-SHA256"
    fields = dict(f.strip().split("=", 1) for f in rest.split(","))
    _key, day, region, service, _ = fields["Credential"].split("/")
    lower = {k.lower(): " ".join(v.split()) for k, v in req.headers.items()}
    names = fields["SignedHeaders"].split(";")
    path, _, query = req.path.partition("?")
    canonical = "\n".join(
        [
            req.method,
            quote(path, safe="/-_.~"),
            query,
            *(f"{n}:{lower[n]}" for n in names),
            "",
            fields["SignedHeaders"],
            hashlib.sha256(req.body).hexdigest(),
        ]
    )
    stamp = lower["x-amz-date"]
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    k = ("AWS4" + secret).encode()
    for part in (day, region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest(), fields["Signature"]


def test_a_prompt_the_guardrail_blocks_is_refused_with_the_same_reply(
    guarded_proxy, bedrock, agent
):
    bedrock.respond = lambda req: Reply(body=_verdict("GUARDRAIL_INTERVENED"))
    p = guarded_proxy()
    body = p.chat(INSULT, session="s").json()
    assert body == {"reply": REFUSAL, "guarded": False, "input_tokens": 0, "output_tokens": 0}
    assert agent.calls() == []
    assert p.events().count("conduct_refused") == 1
    (call,) = bedrock.calls()
    assert (call.method, call.path) == ("POST", APPLY_PATH)
    assert call.json() == {"source": "INPUT", "content": [{"text": {"text": INSULT}}]}


def test_the_guardrail_call_is_signed_for_bedrock_in_the_region(guarded_proxy, bedrock, agent):
    p = guarded_proxy()
    p.chat("one burrito")
    (call,) = bedrock.calls()
    auth = call.headers.get("Authorization") or call.headers["authorization"]
    key, _day, *scope = auth.split("Credential=")[1].split(",")[0].split("/")
    assert (key, scope) == (KEY_ID, [REGION, "bedrock", "aws4_request"])
    headers = {k.lower(): v for k, v in call.headers.items()}
    assert headers["x-amz-security-token"] == SESSION_TOKEN
    assert "x-amz-security-token" in auth and "x-amz-date" in auth and "host" in auth
    expected, carried = _sigv4_signature(call, SECRET)
    assert carried == expected


def test_a_prompt_the_guardrail_passes_reaches_the_agent(guarded_proxy, bedrock, agent):
    p = guarded_proxy()
    assert p.chat(INSULT).json()["reply"] == "Here is your burrito."
    assert _forwarded_prompts(agent) == [INSULT]
    assert len(bedrock.calls()) == 1
    assert "conduct_refused" not in p.events()


def test_the_guardrail_is_not_asked_about_a_prompt_the_terms_refuse(guarded_proxy, bedrock, agent):
    p = guarded_proxy()
    assert p.chat("one burrito, sh1t").json()["reply"] == REFUSAL
    assert bedrock.calls() == []


@pytest.mark.parametrize(
    "failure",
    [
        Reply(status=500, body={"message": "internal"}),
        Reply(status=403, body={"message": "denied"}),
        Reply(body=b"not json"),
    ],
    ids=["500", "403", "garbage"],
)
def test_a_guardrail_error_lets_the_prompt_through(guarded_proxy, bedrock, agent, failure):
    bedrock.respond = lambda req: failure
    p = guarded_proxy()
    assert p.chat(INSULT).json()["reply"] == "Here is your burrito."
    assert _forwarded_prompts(agent) == [INSULT]
    # Asked once: a retry inside the 1.5 s budget would mostly wait.
    assert len(bedrock.calls()) == 1
    events = p.events()
    assert events.count("conduct_guardrail_error") == 1
    assert "conduct_refused" not in events


def test_a_guardrail_that_takes_longer_than_1_5_s_lets_the_prompt_through(
    guarded_proxy, bedrock, agent
):
    bedrock.respond = lambda req: Reply(body=_verdict("GUARDRAIL_INTERVENED"), delay=4.0)
    p = guarded_proxy()
    t0 = time.monotonic()
    assert p.chat(INSULT).json()["reply"] == "Here is your burrito."
    took = time.monotonic() - t0
    assert 1.4 < took < 3.5, f"the turn took {took:.2f}s"
    assert _forwarded_prompts(agent) == [INSULT]
    assert p.events().count("conduct_guardrail_error") == 1


def test_no_guardrail_configured_asks_bedrock_nothing(guarded_proxy, bedrock, agent):
    p = guarded_proxy(CONDUCT_GUARDRAIL_ID=None, CONDUCT_GUARDRAIL_VERSION=None)
    p.chat(INSULT)
    assert bedrock.calls() == []
    assert _forwarded_prompts(agent) == [INSULT]


def test_pod_identity_credentials_sign_the_guardrail_call(guarded_proxy, bedrock, agent, tmp_path):
    # EKS Pod Identity: a loopback credentials endpoint, its authorization token in a file.
    token = tmp_path / "eks-pod-identity-token"
    token.write_text("pod-identity-token")
    expires = (datetime.now(UTC) + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    creds = FakeUpstream(
        lambda req: Reply(
            body={
                "AccessKeyId": "ASIAPODIDENTITY",
                "SecretAccessKey": "pod-secret",
                "Token": "pod-session",
                "Expiration": expires,
            }
        )
    )
    try:
        p = guarded_proxy(
            AWS_ACCESS_KEY_ID=None,
            AWS_SECRET_ACCESS_KEY=None,
            AWS_SESSION_TOKEN=None,
            AWS_CONTAINER_CREDENTIALS_FULL_URI=creds.url + "/v1/credentials",
            AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE=str(token),
        )
        bedrock.respond = lambda req: Reply(body=_verdict("GUARDRAIL_INTERVENED"))
        assert p.chat(INSULT).json()["reply"] == REFUSAL
    finally:
        creds.close()
    (fetch,) = creds.calls()
    assert {k.lower(): v for k, v in fetch.headers.items()}["authorization"] == "pod-identity-token"
    (call,) = bedrock.calls()
    assert "Credential=ASIAPODIDENTITY/" in (
        call.headers.get("Authorization") or call.headers["authorization"]
    )
    expected, carried = _sigv4_signature(call, "pod-secret")
    assert carried == expected


@pytest.mark.spans
def test_a_prompt_the_guardrail_blocks_leaves_only_redacted_on_the_spans(
    traced, bedrock, agent, tmp_path
):
    bedrock.respond = lambda req: Reply(body=_verdict("GUARDRAIL_INTERVENED"))
    p, collector = traced(
        CONDUCT_GUARDRAIL_ID=GUARDRAIL_ID,
        CONDUCT_GUARDRAIL_VERSION=GUARDRAIL_VERSION,
        AWS_REGION=REGION,
        AWS_ENDPOINT_URL_BEDROCK_RUNTIME=bedrock.url,
        AWS_ACCESS_KEY_ID=KEY_ID,
        AWS_SECRET_ACCESS_KEY=SECRET,
        AWS_CONFIG_FILE=str(tmp_path / "none"),
        AWS_SHARED_CREDENTIALS_FILE=str(tmp_path / "none"),
        AWS_EC2_METADATA_DISABLED="true",
        **CAPTURE,
    )
    assert p.chat(INSULT).json()["reply"] == REFUSAL
    spans = _turn_spans(collector)
    for s in spans.values():
        assert s["attributes"]["gen_ai.input.messages"] == _envelope("user", "[redacted]")
    assert all("clown" not in json.dumps(s) for s in collector.spans())


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"CONDUCT_GUARDRAIL_VERSION": None}, "CONDUCT_GUARDRAIL_VERSION"),
        ({"AWS_REGION": None}, "AWS_REGION"),
    ],
    ids=["no-version", "no-region"],
)
def test_a_half_configured_guardrail_stops_startup(guarded_proxy, env, named):
    with pytest.raises(RuntimeError, match="exited") as err:
        guarded_proxy(**env)
    assert named in str(err.value)
