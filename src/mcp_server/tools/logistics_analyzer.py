"""Logistics and belt saturation analysis."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..models.factory_state import FactoryState, BeltMetrics, AssemblerMetrics
from ..utils.recipe_database import get_recipe_database, RecipeDatabase

logger = logging.getLogger(__name__)


# Belt tier maximum throughput (items/sec)
BELT_TIERS = {
    "mk1": 6,   # Blue belt: 6/sec = 360/min
    "mk2": 12,  # Green belt: 12/sec = 720/min
    "mk3": 30,  # Yellow belt: 30/sec = 1800/min
}


@dataclass
class ThroughputRequirement:
    """Calculated throughput requirement for an item."""

    item_id: int
    item_name: str
    production_rate: float  # items/min
    consumption_rate: float  # items/min
    net_rate: float  # items/min (positive = surplus)
    required_belt_tier: str
    belt_count_needed: int


class LogisticsAnalyzer:
    """Analyze belt and logistics station efficiency."""

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
        item_filter: Optional[List[str]] = None,
        saturation_threshold: float = 95.0,
        include_throughput_analysis: bool = True,
    ) -> Dict[str, Any]:
        """
        Detect belt and logistics bottlenecks.

        Args:
            factory_state: Current factory state
            planet_id: Specific planet to analyze (None = all)
            item_filter: Only analyze belts carrying these items
            saturation_threshold: % of max throughput to flag
            include_throughput_analysis: Calculate throughput requirements

        Returns:
            Saturated belts and logistics issues
        """
        logger.info(f"Analyzing logistics: threshold={saturation_threshold}%")

        saturated_belts: List[Dict[str, Any]] = []
        near_saturation: List[Dict[str, Any]] = []
        all_assemblers: List[AssemblerMetrics] = []

        for pid, planet in factory_state.planets.items():
            if planet_id is not None and pid != planet_id:
                continue

            # Collect assemblers for throughput analysis
            all_assemblers.extend(planet.assemblers)

            for belt in planet.belts:
                # Try to resolve item name from ID
                item_display = belt.item_type
                if belt.item_type.startswith("item_"):
                    try:
                        item_id = int(belt.item_type.replace("item_", ""))
                        resolved_name = self.db.get_item_name(item_id)
                        if not resolved_name.startswith("item_"):
                            item_display = resolved_name
                    except (ValueError, TypeError):
                        pass

                # Apply item filter if specified
                if item_filter and item_display not in item_filter and belt.item_type not in item_filter:
                    continue

                belt_data = {
                    "planet_id": pid,
                    "belt_id": belt.belt_id,
                    "item": item_display,
                    "throughput": round(belt.throughput, 2),
                    "max_throughput": belt.max_throughput,
                    "saturation": round(belt.saturation_percent, 1),
                }

                if belt.saturation_percent >= saturation_threshold:
                    belt_data["status"] = "saturated"
                    belt_data["recommendation"] = self._upgrade_recommendation(belt)
                    saturated_belts.append(belt_data)
                elif belt.saturation_percent >= saturation_threshold - 10:
                    belt_data["status"] = "near_saturation"
                    near_saturation.append(belt_data)

        # Sort by saturation level
        saturated_belts.sort(key=lambda b: b["saturation"], reverse=True)
        near_saturation.sort(key=lambda b: b["saturation"], reverse=True)

        result: Dict[str, Any] = {
            "timestamp": factory_state.timestamp.isoformat(),
            "threshold": saturation_threshold,
            "summary": {
                "saturated_count": len(saturated_belts),
                "near_saturation_count": len(near_saturation),
            },
            "saturated_belts": saturated_belts[:20],  # Top 20
            "near_saturation": near_saturation[:10],  # Top 10
            "recommendations": self._global_recommendations(saturated_belts),
        }

        # Add throughput requirement analysis
        if include_throughput_analysis:
            requirements = self._calculate_throughput_requirements(all_assemblers)
            if requirements:
                result["throughput_requirements"] = [
                    {
                        "item": r.item_name,
                        "item_id": r.item_id,
                        "production_rate": round(r.production_rate, 2),
                        "consumption_rate": round(r.consumption_rate, 2),
                        "net_rate": round(r.net_rate, 2),
                        "required_belt_tier": r.required_belt_tier,
                        "belt_count_needed": r.belt_count_needed,
                    }
                    for r in sorted(requirements, key=lambda x: abs(x.net_rate), reverse=True)[:15]
                ]

        return result

    def _calculate_throughput_requirements(
        self,
        assemblers: List[AssemblerMetrics]
    ) -> List[ThroughputRequirement]:
        """Calculate throughput requirements based on production rates."""
        # Aggregate production and consumption by item
        production_by_item: Dict[int, float] = {}
        consumption_by_item: Dict[int, float] = {}

        for assembler in assemblers:
            recipe = self.db.get_recipe(assembler.recipe_id)
            if not recipe:
                continue

            # Track production output
            for output in recipe.outputs:
                production_by_item[output.item_id] = production_by_item.get(
                    output.item_id, 0
                ) + assembler.production_rate

            # Calculate consumption based on recipe inputs
            if assembler.production_rate > 0 and recipe.time > 0:
                cycles_per_min = assembler.production_rate / recipe.primary_output.count
                for inp in recipe.inputs:
                    consumption = cycles_per_min * inp.count
                    consumption_by_item[inp.item_id] = consumption_by_item.get(
                        inp.item_id, 0
                    ) + consumption

        # Build requirements list
        all_items = set(production_by_item.keys()) | set(consumption_by_item.keys())
        requirements: List[ThroughputRequirement] = []

        for item_id in all_items:
            production = production_by_item.get(item_id, 0)
            consumption = consumption_by_item.get(item_id, 0)
            net_rate = production - consumption

            # Calculate belt requirements based on max flow (production or consumption)
            max_flow = max(production, consumption)
            flow_per_sec = max_flow / 60

            # Determine required belt tier
            if flow_per_sec <= BELT_TIERS["mk1"]:
                required_tier = "mk1"
                belt_count = max(1, int(flow_per_sec / BELT_TIERS["mk1"]) + 1) if flow_per_sec > 0 else 0
            elif flow_per_sec <= BELT_TIERS["mk2"]:
                required_tier = "mk2"
                belt_count = max(1, int(flow_per_sec / BELT_TIERS["mk2"]) + 1) if flow_per_sec > 0 else 0
            else:
                required_tier = "mk3"
                belt_count = max(1, int(flow_per_sec / BELT_TIERS["mk3"]) + 1) if flow_per_sec > 0 else 0

            requirements.append(ThroughputRequirement(
                item_id=item_id,
                item_name=self.db.get_item_name(item_id),
                production_rate=production,
                consumption_rate=consumption,
                net_rate=net_rate,
                required_belt_tier=required_tier,
                belt_count_needed=belt_count,
            ))

        return requirements

    def _upgrade_recommendation(self, belt: Any) -> str:
        """Generate upgrade recommendation for a belt."""
        current_tier = self._detect_tier(belt.max_throughput)

        if current_tier == "mk1":
            return "Upgrade to Mk2 (green) belt for 2x throughput"
        elif current_tier == "mk2":
            return "Upgrade to Mk3 (yellow) belt for 2.5x throughput"
        else:
            return "At max tier - consider parallel belt lines"

    def _detect_tier(self, max_throughput: float) -> str:
        """Detect belt tier from max throughput."""
        if max_throughput <= 6:
            return "mk1"
        elif max_throughput <= 12:
            return "mk2"
        else:
            return "mk3"

    def _global_recommendations(
        self, saturated_belts: List[Dict[str, Any]]
    ) -> List[str]:
        """Generate global logistics recommendations."""
        recommendations: List[str] = []

        if len(saturated_belts) == 0:
            recommendations.append("No saturated belts detected - logistics healthy")
        elif len(saturated_belts) < 5:
            recommendations.append(
                f"{len(saturated_belts)} saturated belts - targeted upgrades recommended"
            )
        else:
            recommendations.append(
                f"{len(saturated_belts)} saturated belts - consider systematic belt upgrade"
            )

            # Find most common saturated item
            item_counts: Dict[str, int] = {}
            for belt in saturated_belts:
                item = belt["item"]
                item_counts[item] = item_counts.get(item, 0) + 1

            if item_counts:
                worst_item = max(item_counts, key=item_counts.get)  # type: ignore
                recommendations.append(
                    f"Most congested item: {worst_item} ({item_counts[worst_item]} belts)"
                )

        return recommendations
