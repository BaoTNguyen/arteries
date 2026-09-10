-- Finding 16, half of it: which vocabularies a scope is allowed to ground
-- against.
--
-- `ontology_terms` is one flat table and `_lookup` reads all of it, so a
-- vocabulary loaded for one domain grounds names in every other. Load a finance
-- ontology and "position" inside a coding scope resolves to a securities
-- holding -- a false identity, silently, in the layer whose entire job is
-- deciding when two names mean the same thing.
--
-- Empty means unrestricted, so this changes nothing until someone binds a scope.
CREATE TABLE IF NOT EXISTS arteries.ontology_bindings (
    scope_id    TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, source)
);
