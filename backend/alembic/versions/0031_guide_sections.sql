-- 0031: the guide plan names a section for each page (guide-plan schema 2.0.0); pages planned before
-- it have none ('').
ALTER TABLE guide_pages ADD COLUMN section text NOT NULL DEFAULT '' CHECK (length(section) <= 60);
