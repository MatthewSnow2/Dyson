"""Production bottleneck detection and analysis."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..models.factory_state import FactoryState, AssemblerMetrics, PlanetState
from ..utils.recipe_database import get_recipe_database, RecipeDatabase

logger = logging.getLogger(__name__)


@dataclass
class Bottleneck:
    """Represents a detected production bottleneck."""

    item_id: int
    item_name: str
    recipe_id: int
    bottleneck_type: str  # "input_starvation", "output_blocked", "power_limited", "low_efficiency"
    severity: float  # 0-100, higher = more severe
    affected_throughput: float  # items/min lost
    efficiency: float  # current efficiency %
    root_cause: str
    recommendation: str
    upstream_items: List[str]  # Items causing this bottleneck
    downstream_impact: List[str]  # Items affected by this bottleneck
    planet_id: int
    assembler_count: int = 1


class BottleneckAnalyzer:
    """
    Analyze factory state to identify production bottlenecks.

    Uses recipe database to:
    - Map recipe IDs to item names
    - Calculate theoretical production rates
    - Trace dependency chains for root cause analysis
    - Identify downstream impact
    """

    def __init__(self) -> None:
        self._db: Optional[RecipeDatabase] = None

    @property
    def db(self) -> RecipeDatabase:
        """Get recipe database (lazy load)."""
        if self._db is None:
            self._db = get_recipe_database()
        return self._db

    async def analyze(
        self,
        factory_state: FactoryState,
        planet_id: Optional[int] = None,
        target_item: Optional[str] = None,
        time_window: int = 60,
        include_downstream: bool = True,
    ) -> Dict[str, Any]:
        """
        Identify production chain bottlenecks.

        Args:
            factory_state: Current factory state
            planet_id: Specific planet to analyze (None = all)
            target_item: Focus on specific product
            time_window: Analysis window in seconds
            include_downstream: Trace impact to final products

        Returns:
            Analysis results with bottlenecks and recommendations
        """
        logger.info(f"Analyzing bottlenecks: planet={planet_id}, item={target_item}")

        bottlenecks: List[Bottleneck] = []
        planets_analyzed = 0
        total_assemblers = 0
        inefficient_assemblers = 0

        # Get target item ID if specified
        target_item_id: Optional[int] = None
        if target_item:
            target_item_id = self.db.get_item_id(target_item)

        for pid, planet in factory_state.planets.items():
            if planet_id is not None and pid != planet_id:
                continue

            planets_analyzed += 1
            total_assemblers += len(planet.assemblers)

            # Group assemblers by recipe
            assemblers_by_recipe: Dict[int, List[AssemblerMetrics]] = {}
            for assembler in planet.assemblers:
                if assembler.recipe_id not in assemblers_by_recipe:
                    assemblers_by_recipe[assembler.recipe_id] = []
                assemblers_by_recipe[assembler.recipe_id].append(assembler)

            # Analyze each recipe group
            for recipe_id, recipe_assemblers in assemblers_by_recipe.items():
                recipe = self.db.get_recipe(recipe_id)
                if not recipe:
                    continue

                # Filter by target item if specified
                if target_item_id is not None:
                    if recipe.primary_output_id != target_item_id:
                        # Check if target is in the dependency chain
                        if not self._is_in_dependency_chain(recipe_id, target_item_id):
                            continue

                # Analyze this recipe group
                group_bottleneck = self._analyze_recipe_group(
                    recipe_id=recipe_id,
                    assemblers=recipe_assemblers,
                    planet=planet,
                    include_downstream=include_downstream,
                )

                if group_bottleneck:
                    group_bottleneck.planet_id = pid
                    bottlenecks.append(group_bottleneck)

                # Count inefficient assemblers
                for asm in recipe_assemblers:
                    if asm.efficiency < 90:
                        inefficient_assemblers += 1

        # Sort by severity
        bottlenecks.sort(key=lambda b: b.severity, reverse=True)

        # Build critical path
        critical_path = self._build_critical_path(bottlenecks, target_item_id) if include_downstream else []

        # Generate summary statistics
        summary = self._generate_summary(bottlenecks, total_assemblers, inefficient_assemblers)

        return {
            "timestamp": factory_state.timestamp.isoformat(),
            "planets_analyzed": planets_analyzed,
            "total_assemblers": total_assemblers,
            "inefficient_assemblers": inefficient_assemblers,
            "bottlenecks_found": len(bottlenecks),
            "summary": summary,
            "bottlenecks": [
                {
                    "item": b.item_name,
                    "item_id": b.item_id,
                    "recipe_id": b.recipe_id,
                    "planet_id": b.planet_id,
                    "type": b.bottleneck_type,
                    "severity": round(b.severity, 1),
                    "efficiency": round(b.efficiency, 1),
                    "throughput_loss": round(b.affected_throughput, 2),
                    "assembler_count": b.assembler_count,
                    "root_cause": b.root_cause,
                    "recommendation": b.recommendation,
                    "upstream_items": b.upstream_items,
                    "downstream_impact": b.downstream_impact,
                }
                for b in bottlenecks[:10]  # Top 10 bottlenecks
            ],
            "critical_path": critical_path,
        }

    def _analyze_recipe_group(
        self,
        recipe_id: int,
        assemblers: List[AssemblerMetrics],
        planet: PlanetState,
        include_downstream: bool,
    ) -> Optional[Bottleneck]:
        """Analyze a group of assemblers running the same recipe."""
        recipe = self.db.get_recipe(recipe_id)
        if not recipe:
            return None

        # Calculate group statistics
        total_production = sum(a.production_rate for a in assemblers)
        total_theoretical = sum(a.theoretical_max for a in assemblers) if assemblers[0].theoretical_max > 0 else 0

        # If no theoretical max set, calculate from recipe
        if total_theoretical == 0:
            total_theoretical = self.db.calculate_theoretical_rate(recipe_id, len(assemblers))

        avg_efficiency = (total_production / total_theoretical * 100) if total_theoretical > 0 else 100

        # Check for bottleneck conditions
        input_starved_count = sum(1 for a in assemblers if a.input_starved)
        output_blocked_count = sum(1 for a in assemblers if a.output_blocked)

        # Determine bottleneck type and severity
        bottleneck_type: Optional[str] = None
        severity = 0.0
        root_cause = ""
        recommendation = ""

        if input_starved_count > len(assemblers) * 0.3:  # >30% starved
            bottleneck_type = "input_starvation"
            severity = (input_starved_count / len(assemblers)) * 100
            upstream = self._get_upstream_items(recipe_id)
            root_cause = f"Insufficient input: {', '.join(upstream[:3])}"
            recommendation = f"Increase production of {upstream[0] if upstream else 'inputs'} or add more input belts"

        elif output_blocked_count > len(assemblers) * 0.3:  # >30% blocked
            bottleneck_type = "output_blocked"
            severity = (output_blocked_count / len(assemblers)) * 100
            downstream = self._get_downstream_items(recipe.primary_output_id) if include_downstream else []
            root_cause = "Output buffer full, downstream consumption insufficient"
            if downstream:
                recommendation = f"Increase consumption by {downstream[0]} or add more output belts"
            else:
                recommendation = "Add more output belts or increase downstream consumption"

        elif avg_efficiency < 80:
            bottleneck_type = "low_efficiency"
            severity = 100 - avg_efficiency

            # Check power status
            if planet.power and planet.power.surplus_mw < 0:
                root_cause = f"Power deficit of {abs(planet.power.surplus_mw):.1f}MW limiting production"
                recommendation = "Add power generation to restore full efficiency"
            else:
                root_cause = "Assemblers running below optimal efficiency"
                recommendation = "Check for sporadic input/output issues or power fluctuations"

        if bottleneck_type is None:
            return None

        # Get upstream and downstream items
        upstream_items = self._get_upstream_items(recipe_id)
        downstream_items = self._get_downstream_items(recipe.primary_output_id) if include_downstream else []

        return Bottleneck(
            item_id=recipe.primary_output_id,
            item_name=recipe.primary_output.item_name or self.db.get_item_name(recipe.primary_output_id),
            recipe_id=recipe_id,
            bottleneck_type=bottleneck_type,
            severity=severity,
            affected_throughput=total_theoretical - total_production,
            efficiency=avg_efficiency,
            root_cause=root_cause,
            recommendation=recommendation,
            upstream_items=upstream_items[:5],
            downstream_impact=downstream_items[:5],
            planet_id=0,  # Will be set by caller
            assembler_count=len(assemblers),
        )

    def _get_upstream_items(self, recipe_id: int) -> List[str]:
        """Get list of upstream input items for a recipe."""
        recipe = self.db.get_recipe(recipe_id)
        if not recipe:
            return []
        return [inp.item_name or self.db.get_item_name(inp.item_id) for inp in recipe.inputs]

    def _get_downstream_items(self, item_id: int) -> List[str]:
        """Get list of downstream items that use this item."""
        downstream = self.db.trace_bottleneck_downstream(item_id, max_depth=3)
        return [name for _, name in downstream]

    def _is_in_dependency_chain(self, recipe_id: int, target_item_id: int) -> bool:
        """Check if a recipe produces something in the dependency chain of target."""
        recipe = self.db.get_recipe(recipe_id)
        if not recipe:
            return False

        # Check if this recipe's output is needed for target
        downstream = self.db.trace_bottleneck_downstream(recipe.primary_output_id, max_depth=5)
        return any(item_id == target_item_id for item_id, _ in downstream)

    def _build_critical_path(
        self,
        bottlenecks: List[Bottleneck],
        target_item_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        """
        Build critical path showing the chain of bottlenecks.

        The critical path shows how bottlenecks cascade through
        the production chain.
        """
        if not bottlenecks:
            return []

        # Group bottlenecks by severity
        critical_path: List[Dict[str, Any]] = []

        # Start with highest severity bottleneck
        root = bottlenecks[0]

        # Trace upstream to find root cause
        upstream = self.db.trace_bottleneck_upstream(root.item_id, max_depth=5)

        for item_id, item_name, recipe_id in upstream:
            # Check if this item has a bottleneck
            matching_bottleneck = next(
                (b for b in bottlenecks if b.item_id == item_id),
                None
            )

            step = {
                "item": item_name,
                "item_id": item_id,
                "recipe_id": recipe_id,
                "has_bottleneck": matching_bottleneck is not None,
            }

            if matching_bottleneck:
                step["bottleneck_type"] = matching_bottleneck.bottleneck_type
                step["severity"] = matching_bottleneck.severity

            critical_path.append(step)

        return critical_path

    def _generate_summary(
        self,
        bottlenecks: List[Bottleneck],
        total_assemblers: int,
        inefficient_assemblers: int,
    ) -> Dict[str, Any]:
        """Generate summary statistics for the analysis."""
        if not bottlenecks:
            return {
                "status": "healthy",
                "message": "No significant bottlenecks detected",
                "efficiency": 100.0 if total_assemblers == 0 else
                    round((1 - inefficient_assemblers / total_assemblers) * 100, 1),
            }

        # Categorize bottlenecks
        by_type: Dict[str, int] = {}
        for b in bottlenecks:
            by_type[b.bottleneck_type] = by_type.get(b.bottleneck_type, 0) + 1

        most_common = max(by_type, key=by_type.get) if by_type else "unknown"
        most_severe = bottlenecks[0]

        status = "critical" if most_severe.severity > 80 else "warning" if most_severe.severity > 50 else "minor"

        return {
            "status": status,
            "total_bottlenecks": len(bottlenecks),
            "most_common_type": most_common,
            "most_severe_item": most_severe.item_name,
            "most_severe_type": most_severe.bottleneck_type,
            "efficiency": round((1 - inefficient_assemblers / max(1, total_assemblers)) * 100, 1),
            "message": self._generate_summary_message(most_severe, len(bottlenecks)),
        }

    def _generate_summary_message(self, most_severe: Bottleneck, count: int) -> str:
        """Generate human-readable summary message."""
        if most_severe.bottleneck_type == "input_starvation":
            return (
                f"Production of {most_severe.item_name} is limited by input availability. "
                f"Upstream production of {most_severe.upstream_items[0] if most_severe.upstream_items else 'inputs'} "
                f"needs to be increased."
            )
        elif most_severe.bottleneck_type == "output_blocked":
            return (
                f"Production of {most_severe.item_name} is backing up due to insufficient downstream consumption. "
                f"Consider adding more output capacity or increasing demand."
            )
        elif most_severe.bottleneck_type == "low_efficiency":
            return (
                f"{count} production lines running below optimal efficiency. "
                f"Check power supply and belt saturation."
            )
        else:
            return f"{count} bottlenecks detected affecting factory throughput."
