"""Power grid analysis and optimization."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..models.factory_state import FactoryState, AssemblerMetrics
from ..utils.recipe_database import get_recipe_database, RecipeDatabase

logger = logging.getLogger(__name__)


# Power consumption per building type (MW at full load)
BUILDING_POWER_CONSUMPTION = {
    "smelter": {"mk1": 0.36, "mk2": 0.72},
    "assembler": {"mk1": 0.27, "mk2": 0.54, "mk3": 1.08},
    "chemical": {"mk1": 0.72, "mk2": 1.44},
    "refinery": {"mk1": 0.96, "mk2": 1.92},
    "particle": {"mk1": 12.0, "mk2": 24.0},  # Particle collider
    "lab": {"mk1": 0.48, "mk2": 0.96},
    "mining": {"mk1": 0.42, "mk2": 0.84, "mk3": 1.68},
    "oil_extractor": {"mk1": 0.84, "mk2": 1.68},
    "fractionator": {"mk1": 0.72},
    "orbital_collector": {"mk1": 0},  # Solar powered
    "ray_receiver": {"mk1": -15.0},  # Generates power
}


@dataclass
class PowerConsumer:
    """Represents a power-consuming production line."""

    recipe_id: int
    item_name: str
    building_type: str
    building_count: int
    power_mw: float
    efficiency: float
    production_rate: float


class PowerAnalyzer:
    """Analyze power grid efficiency and identify issues."""

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
        include_accumulator_cycles: bool = True,
        include_consumers: bool = True,
    ) -> Dict[str, Any]:
        """
        Evaluate power generation, consumption, and distribution.

        Args:
            factory_state: Current factory state
            planet_id: Specific planet to analyze (None = all)
            include_accumulator_cycles: Include charge/discharge analysis
            include_consumers: Include power consumption breakdown by production line

        Returns:
            Power grid analysis with recommendations
        """
        logger.info(f"Analyzing power grid: planet={planet_id}")

        planets_data: List[Dict[str, Any]] = []
        total_generation = 0.0
        total_consumption = 0.0
        deficits_found = 0
        all_consumers: List[PowerConsumer] = []

        for pid, planet in factory_state.planets.items():
            if planet_id is not None and pid != planet_id:
                continue

            if planet.power is None:
                continue

            power = planet.power
            total_generation += power.generation_mw
            total_consumption += power.consumption_mw

            planet_data: Dict[str, Any] = {
                "planet_id": pid,
                "planet_name": planet.planet_name,
                "generation_mw": round(power.generation_mw, 2),
                "consumption_mw": round(power.consumption_mw, 2),
                "surplus_mw": round(power.surplus_mw, 2),
                "status": "surplus" if power.surplus_mw >= 0 else "deficit",
            }

            if power.surplus_mw < 0:
                deficits_found += 1
                planet_data["recommendation"] = self._generate_recommendation(power)

            if include_accumulator_cycles:
                planet_data["accumulator_charge"] = f"{power.accumulator_charge_percent:.1f}%"

            # Analyze power consumers (production lines)
            if include_consumers:
                consumers = self._analyze_power_consumers(planet.assemblers, pid)
                all_consumers.extend(consumers)
                if consumers:
                    planet_data["top_consumers"] = [
                        {
                            "item": c.item_name,
                            "power_mw": round(c.power_mw, 2),
                            "building_count": c.building_count,
                            "efficiency": round(c.efficiency, 1),
                        }
                        for c in sorted(consumers, key=lambda x: x.power_mw, reverse=True)[:5]
                    ]

            planets_data.append(planet_data)

        # Build result
        result: Dict[str, Any] = {
            "timestamp": factory_state.timestamp.isoformat(),
            "summary": {
                "total_generation_mw": round(total_generation, 2),
                "total_consumption_mw": round(total_consumption, 2),
                "net_surplus_mw": round(total_generation - total_consumption, 2),
                "planets_with_deficit": deficits_found,
            },
            "planets": planets_data,
            "recommendations": self._global_recommendations(total_generation, total_consumption),
        }

        # Add global power consumers breakdown
        if include_consumers and all_consumers:
            result["power_breakdown"] = self._generate_power_breakdown(all_consumers)

        return result

    def _analyze_power_consumers(
        self,
        assemblers: List[AssemblerMetrics],
        planet_id: int,
    ) -> List[PowerConsumer]:
        """Analyze power consumption by production line."""
        consumers_by_recipe: Dict[int, PowerConsumer] = {}

        for assembler in assemblers:
            recipe = self.db.get_recipe(assembler.recipe_id)
            if not recipe:
                continue

            building_type = recipe.building
            power_per_building = BUILDING_POWER_CONSUMPTION.get(
                building_type, {}
            ).get("mk2", 0.5)  # Default to mk2

            if assembler.recipe_id not in consumers_by_recipe:
                consumers_by_recipe[assembler.recipe_id] = PowerConsumer(
                    recipe_id=assembler.recipe_id,
                    item_name=recipe.primary_output.item_name or self.db.get_item_name(recipe.primary_output_id),
                    building_type=building_type,
                    building_count=0,
                    power_mw=0.0,
                    efficiency=0.0,
                    production_rate=0.0,
                )

            consumer = consumers_by_recipe[assembler.recipe_id]
            consumer.building_count += 1
            consumer.power_mw += power_per_building * (assembler.efficiency / 100)
            consumer.production_rate += assembler.production_rate

        # Calculate average efficiency
        for consumer in consumers_by_recipe.values():
            if consumer.building_count > 0:
                consumer.efficiency = (
                    consumer.power_mw / (consumer.building_count * BUILDING_POWER_CONSUMPTION.get(
                        consumer.building_type, {}
                    ).get("mk2", 0.5)) * 100
                )

        return list(consumers_by_recipe.values())

    def _generate_power_breakdown(
        self,
        consumers: List[PowerConsumer]
    ) -> Dict[str, Any]:
        """Generate power consumption breakdown."""
        # Group by building type
        by_building: Dict[str, float] = {}
        for consumer in consumers:
            by_building[consumer.building_type] = by_building.get(
                consumer.building_type, 0
            ) + consumer.power_mw

        total_tracked = sum(c.power_mw for c in consumers)

        # Top consumers
        top_consumers = sorted(consumers, key=lambda x: x.power_mw, reverse=True)[:10]

        return {
            "by_building_type": {k: round(v, 2) for k, v in sorted(
                by_building.items(), key=lambda x: x[1], reverse=True
            )},
            "top_power_consumers": [
                {
                    "item": c.item_name,
                    "recipe_id": c.recipe_id,
                    "building_type": c.building_type,
                    "building_count": c.building_count,
                    "power_mw": round(c.power_mw, 2),
                    "production_rate": round(c.production_rate, 2),
                }
                for c in top_consumers
            ],
            "total_tracked_mw": round(total_tracked, 2),
        }

    def _generate_recommendation(self, power: Any) -> str:
        """Generate power recommendation for a planet."""
        deficit = abs(power.surplus_mw)

        if deficit < 10:
            return f"Minor deficit of {deficit:.1f}MW - add 1 thermal plant"
        elif deficit < 50:
            plants_needed = int(deficit / 15) + 1  # Assuming ~15MW per fusion
            return f"Deficit of {deficit:.1f}MW - add {plants_needed} fusion plants"
        else:
            return f"Major deficit of {deficit:.1f}MW - consider artificial sun or ray receivers"

    def _global_recommendations(
        self, generation: float, consumption: float
    ) -> List[str]:
        """Generate global power recommendations."""
        recommendations: List[str] = []
        surplus = generation - consumption
        surplus_percent = (surplus / consumption * 100) if consumption > 0 else 100

        if surplus < 0:
            recommendations.append(
                f"CRITICAL: Global power deficit of {abs(surplus):.1f}MW"
            )
        elif surplus_percent < 10:
            recommendations.append(
                f"WARNING: Power surplus below 10% ({surplus_percent:.1f}%)"
            )
            recommendations.append("Consider adding generation capacity before expanding")
        elif surplus_percent > 50:
            recommendations.append(
                f"Healthy power surplus of {surplus_percent:.1f}%"
            )

        return recommendations
