-- The schema as of the ingestion redesign branch point. Empty on purpose.
--
-- schema.sql is applied by `art setup`, so on a fresh database everything below
-- version 002 already exists. This file gives `art migrate baseline` something
-- to stamp and gives the runner a non-empty directory to order against. Real
-- migrations start at 002.
SELECT 1;
