-- Expand only. Provenance for ephemeral rows: 'untrusted' when an unattended
-- agent, or an assistant turn in a session that used a web tool, wrote it.
-- NULL is trusted, which is what every existing row was written as.
-- persistent carries the same bit in source_meta->>'trust' and needs no column.
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS trust TEXT;
