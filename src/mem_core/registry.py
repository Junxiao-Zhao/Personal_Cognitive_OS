from __future__ import annotations

from .profile import ProfileRegistry


def default_registry() -> ProfileRegistry:
    registry = ProfileRegistry()
    registry.register_lazy("pco.validate", "pco.validation:validate_profile")
    registry.register_lazy("pco.retrieval.search", "pco.retrieval:search")
    registry.register_lazy("pco.backlinks.build", "pco.backlinks:build")
    registry.register_lazy("pco.context.render", "pco.context:render")
    registry.register_lazy("pco.index.build", "pco.retrieval:build_index")
    registry.register_lazy("pco.projection.markdown", "pco.projections:project_markdown")
    registry.register_lazy("pco.projection.affine", "pco.projections:project_affine")
    registry.register_lazy("research.validate", "pco.research_profile:validate_profile")
    registry.register_lazy("research.retrieval.search", "pco.research_profile:search")
    registry.register_lazy("research.projection.markdown", "pco.research_profile:project_markdown")
    return registry
