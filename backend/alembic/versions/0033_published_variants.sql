-- 0033: the variant builds a publish approved (same family and reader level, each through the review gate
-- and the publish's own acknowledgment); empty for publications made before.
ALTER TABLE publications ADD COLUMN variant_build_ids uuid[] NOT NULL DEFAULT '{}';
