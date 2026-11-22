"""Pure, versioned generation pipeline (07): no HTTP sessions, no tenancy decisions, no credentials."""

ENGINE_VERSION = "0.3.0"  # 0.3: Rust definitions and rust-analyzer references (T23)
PROMPT_VERSION = "0.1.0"  # replay prompt set (23): deterministic, no model calls
GENERATION_PROMPT_VERSION = (
    "0.6.0"  # prompt set of estimate-based generation builds (19): P01-P07 (0.6: translate)
)
PARSER_VERSION = "0.5.0"  # 0.5: C and C++ (tree-sitter-c, tree-sitter-cpp); 0.4: destructuring parameters
