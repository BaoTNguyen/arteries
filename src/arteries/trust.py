"""Where a memory came from, and who may read it.

Everything else in arteries grades a claim by quality: is it worth keeping
(promote), how strongly is it known (evidence), is anyone reading it (evict).
None of that asks where the words came from, and every one of those filters is
aimed at noise -- content written to pass a quality bar passes it. A page an
agent read can say "this project's convention is X" in exactly the voice a real
convention has.

So provenance is its own axis, with one bit that matters: `untrusted`. A row is
untrusted when the text was produced where the web could reach it --

  * by an agent heart or plexus ran unattended (ARTERIES_TRUST=untrusted, set
    in the agent's environment), or
  * by the assistant in an interactive session that used a web tool.

The bit is sticky. Compile never mixes trust classes in one batch (a model
told to attribute facts could otherwise launder an injected claim onto a
trusted record), an untrusted batch cannot retire a trusted claim, and nothing
untrusted is promoted to evergreen.

And it is resolved at compile, not left waiting. A filter cannot turn an
unverified claim into a trusted one -- content written to pass a filter passes
it, which is the whole reason this module exists -- so resolution never means
"now trusted". Every untrusted fact leaves compile as one of:

  dropped       it has the shape of steering (steering() below): a command, a
                URL or package, a credential, a way around a check, an order
                addressed to whoever reads it. Never stored.
  corroborated  it restates a trusted claim (CORROBORATE_SIM). Nothing new, so
                nothing is written; the trusted claim already says it.
  short-lived   anything else: stored, visible only to agents in its own
                project, and gone after TTL_DAYS. Long enough to carry research
                from one attempt to the next, not long enough to become lore.

`art trust promote` is still there for the rare claim worth keeping; it
records `reviewed` rather than erasing the history.

Readers:

  * an interactive session (no ARTERIES_TRUST) never sees untrusted rows --
    that is the session with your credentials and your permissions;
  * an agent run sees them only from its own project, where they came from;
  * a web-lane agent (ARTERIES_LANE=web) sees nothing from another project at
    all, trusted or not: whatever it is handed it can send anywhere, and the
    shared scope holds notes from every project and every host session.
"""
from __future__ import annotations

import datetime
import os
import re
import sys

UNTRUSTED = "untrusted"
REVIEWED = "reviewed"

#: Tool names that fetch from the open web, lower-cased: Claude Code's pair,
#: and the snake_case spelling other CLIs use.
WEB_TOOLS = ("webfetch", "websearch", "web_fetch", "web_search")


TTL_DAYS = float(os.getenv("ARTERIES_UNTRUSTED_TTL_DAYS", "3"))
# Below compile.DUPLICATE_SIM (0.97, near-identical strings) and above the 0.75
# "related" band: the same claim in other words.
CORROBORATE_SIM = float(os.getenv("ARTERIES_CORROBORATE_SIM", "0.92"))

# What injected text needs a memory to say. Blunt on purpose: the cost of a
# false drop is a short-lived note never written, and the claims these miss
# are still bounded by TTL and isolation -- this is the first fence, not the
# only one.
_STEERING = (
    ("command", re.compile(
        r"`[^`]*\b(curl|wget|bash|sh|sudo|chmod|pip|npm|npx|uv|pnpm|yarn|brew|apt|git)\b[^`]*`"
        r"|\b(curl|wget)\s+\S|\|\s*(ba)?sh\b|\bsudo\s|\bchmod\s+\+?x"
        r"|\b(pip|pip3|uv|npm|pnpm|yarn|brew|apt(-get)?)\s+(install|add|i)\b", re.I)),
    ("url", re.compile(r"\b(?:https?|wss?|ftp)://|\bwww\.|\b[\w-]+\.(?:sh|io|xyz|ru|cn|top|zip)/", re.I)),
    ("credential", re.compile(
        # "token" alone is parser vocabulary; the credential kinds are named
        r"\b(api[_ -]?key|(api|access|auth|bearer|refresh|session|oauth|github|npm|pypi)[_ -]?token"
        r"|password|passwd|secret|credential|private key|ssh key)s?\b"
        r"|\.env\b|\.ssh/|\.aws/|\.netrc|auth\.json", re.I)),
    ("bypass", re.compile(
        r"\b(skip\w*|disabl\w*|bypass\w*|ignor\w*|suppress\w*|turn\w* off)\b.{0,30}\b(tests?|checks?|review|"
        r"hooks?|holds?|sandbox|verification|lint|ci|safety|guard)"
        r"|\b(tests?|checks?|reviews?|hooks?|holds?|sandbox\w*|verification|lint\w*|ci)\b.{0,30}"
        r"\b(skip\w*|bypass\w*|disabl\w*|ignor\w*|suppress\w*|optional|not (needed|required))\b"
        r"|--no-verify|\bpre-?approved\b|\bauto-?approve|\bno need to (review|test|check)", re.I)),
    ("directive", re.compile(
        r"^\s*(always|never|you must|you should|make sure to|do not|don't)\b"
        r"|\b(ignore|disregard) (all |any )?(previous|prior|above|earlier)\b"
        r"|\b(the )?(assistant|agent|model|ai|claude|reviewer)s? (must|should|needs? to|is required to)\b"
        r"|\bIMPORTANT\b|\bsystem prompt\b", re.I | re.M)),
)


def steering(fact: str) -> str | None:
    """Why an untrusted fact is dropped at compile, or None to keep it."""
    for name, pattern in _STEERING:
        if pattern.search(fact or ""):
            return name
    return None


def expiry(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now + datetime.timedelta(days=TTL_DAYS)).isoformat()


def retire_expired(conn) -> int:
    """Tombstone short-lived untrusted rows whose time is up. visible() already
    hides them the moment they expire; this makes it true for every other
    reader too, and shows up in `art trace` as a retirement."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE arteries.persistent SET valid_until = now()
               WHERE valid_until IS NULL AND source_meta->>'trust' = %s
                 AND source_meta ? 'expires'
                 AND (source_meta->>'expires')::timestamptz <= now()""",
            (UNTRUSTED,))
        n = cur.rowcount
    conn.commit()
    return n


def agent_run() -> bool:
    """This process is an unattended agent turn: what it writes is untrusted,
    and it may read untrusted rows from its own project."""
    return os.getenv("ARTERIES_TRUST", "").strip().lower() == UNTRUSTED


def web_lane() -> bool:
    return os.getenv("ARTERIES_LANE", "").strip().lower() == "web"


def reader_params() -> dict:
    """The parameters `visible()` reads, from this process's environment."""
    return {"trust_agent": agent_run(), "trust_web": web_lane()}


def visible(alias: str = "p") -> str:
    """SQL appended to every read that puts persistent rows into a prompt.

    Expects %(project)s -- every such query already has it for the scope CTE --
    plus the keys of reader_params().
    """
    return f"""
              AND (coalesce({alias}.source_meta->>'trust', '') <> '{UNTRUSTED}'
                   OR (%(trust_agent)s AND {alias}.project_id = %(project)s
                       AND coalesce(({alias}.source_meta->>'expires')::timestamptz,
                                    'infinity') > now()))
              AND (NOT %(trust_web)s OR {alias}.project_id = %(project)s)"""


def main(argv: list[str] | None = None) -> int:
    """art trust [list [project]] | art trust promote <id>...

    list shows untrusted live rows, newest first. promote marks rows
    `reviewed`: from then on they read like any other memory. Promote after
    reading the claim, not in bulk -- a promotion is the check this exists for.
    """
    import psycopg2
    import psycopg2.extras

    from arteries.config import DB_CONFIG

    args = list(argv if argv is not None else sys.argv[1:])
    sub = args.pop(0) if args else "list"
    with psycopg2.connect(**DB_CONFIG) as conn, \
            conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if sub == "list":
            cur.execute(
                """SELECT id, project_id, left(fact, 100) AS fact,
                          left(source_meta->>'expires', 16) AS expires
                   FROM arteries.persistent
                   WHERE valid_until IS NULL AND source_meta->>'trust' = %s
                     AND (%s::text IS NULL OR project_id = %s)
                   ORDER BY source_ts DESC LIMIT 100""",
                (UNTRUSTED, args[0] if args else None, args[0] if args else None))
            rows = cur.fetchall()
            for r in rows:
                print(f"{r['id']}  [{r['project_id']}]  until {r['expires'] or '-'}  {r['fact']}")
            print(f"{len(rows)} untrusted live row(s)")
            return 0
        if sub == "promote" and args:
            cur.execute(
                """UPDATE arteries.persistent
                   SET source_meta = (source_meta - 'expires') || jsonb_build_object('trust', %s)
                   WHERE id::text = ANY(%s) AND source_meta->>'trust' = %s""",
                (REVIEWED, args, UNTRUSTED))
            print(f"promoted {cur.rowcount} row(s) to {REVIEWED}")
            return 0
    print("usage: art trust [list [project]] | art trust promote <id>...")
    return 2
