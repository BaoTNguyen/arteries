-- Commit 1 of planning/compaction_v3.md: chaining columns only. No reader
-- changes yet -- `build_packet` does not write or read these until the
-- renderer split (v3 §2) lands. Purely additive, all nullable, so a database
-- on this migration and code on the previous one are both correct.
--
-- `body` lets a packet be read back whole later (v3 §5's `covers_from` /
-- `covers_to` arithmetic needs the previous packet's range, not its text, but
-- storing the text too is what makes "what did the last packet actually say"
-- answerable without re-deriving it). `previous_id` is the chain itself.
--
-- Retraction does NOT get a lifetime column here: corrections render once,
-- mid-session, from the detectors in v3 §4.2, and from then on live in
-- `arteries.persistent` under the existing promotion/supersession workflow
-- (valid_until + a `supersedes` edge, compile.py:826-893). A packet asks
-- persistent what's currently true; it does not maintain its own memory of
-- what it said last time.
ALTER TABLE arteries.packets
    ADD COLUMN IF NOT EXISTS previous_id UUID REFERENCES arteries.packets(id),
    ADD COLUMN IF NOT EXISTS covers_from TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS covers_to   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resume_from TEXT,
    ADD COLUMN IF NOT EXISTS body        TEXT;
