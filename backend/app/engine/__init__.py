"""Pure, versioned generation pipeline (07): no HTTP sessions, no tenancy decisions, no credentials."""

ENGINE_VERSION = "0.2.0"  # 0.2: SCIP-resolved references change ranking and related context (T23)
PROMPT_VERSION = "0.1.0"  # replay prompt set (23): deterministic, no model calls
GENERATION_PROMPT_VERSION = "0.4.0"  # prompt set of estimate-based generation builds (19): P01-P06
PARSER_VERSION = "0.2.0"  # 0.2: the bundle includes the SCIP indexers (scip-python, scip-typescript)
