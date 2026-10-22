"""Backend-for-frontend API (Run 4, Phase 30): serves the engine's exported
web bundle over the contract in docs/api/openapi.yaml. Heavy computation
happens in the engine; the API paginates, filters, sorts and caches - the
browser never recalculates financials."""
