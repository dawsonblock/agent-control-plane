"""Context building (Milestone 6 — Haystack retrieval).

Scans repos + vault, indexes via haystack-ai (sentence-transformers),
and emits a focused context_bundle.md before the agent runs. The
graph's build_context node uses ContextBuilder + HaystackIndexer to
retrieve relevant chunks from prior task notes and the repo itself.

When the ``rag`` optional extra is not installed, the build_context
node degrades gracefully to prompt-only mode (no retrieval, just the
user request + repo metadata).

v0.7.0 (Phase 4.2): Optional cross-encoder re-ranking improves the
signal-to-noise ratio of retrieved chunks. See RerankingSection in
acp.config for configuration.
"""
