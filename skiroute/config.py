"""Configuration objects for skiroute.

All tunables live here. Defaults are calibrated on Tignes / Val d'Isère
and are reasonable starting points for any large Alpine resort. North
American or smaller resorts may want to override `lift_time_by_type`
and the skating-cost tiers.
"""

from dataclasses import dataclass, field
from typing import Literal

DifficultyMode = Literal["prefer-easy", "reds-if-needed", "any-piste"]
RouteObjective = Literal["fastest", "most-skiing"]


@dataclass
class ResortFilter:
    """Selects which slice of a global OpenSkiData GPKG to build.

    All fields are optional but at least one should be set, otherwise
    the builder will try to load every resort on earth.

    Attributes:
        localities: List of locality strings; matched with SQL LIKE.
            e.g. ["Tignes", "Val d'Isère"]
        ski_area_pattern: SQL LIKE pattern matched against ski_area_names.
            e.g. "Tignes%Val d%" or "Whistler%"
        exclude_lift_types: Lift types to skip (e.g. ["magic_carpet"]).
    """
    localities: list[str] = field(default_factory=list)
    ski_area_pattern: str | None = None
    exclude_lift_types: list[str] = field(default_factory=lambda: ["magic_carpet"])


@dataclass
class BuildConfig:
    """Tunables for graph construction.

    Defaults are Tignes-tested. Most resorts will work fine with these.

    Attributes:
        ski_speed_ms: Average skiing speed including turns, used to convert
            run length to time.
        lift_time_by_type: Per-lift-type ride duration in seconds.
        gondola_funicular_cablecar_can_descend: Whether these lift types
            should also produce a downhill (lift_down) edge.
        lift_down_penalty: Extra seconds added to lift_down edges so the
            router prefers skiing when possible.
        skate_cost_flat: Cost (seconds) for a flat skating bridge (≤5m gain).
        skate_cost_moderate: Cost for a 5–15m uphill skate.
        skate_cost_steep: Cost for a >15m uphill skate; strongly discouraged.
        max_gap_distance_m: Maximum distance between two nodes to consider
            bridging them with a skating edge.
        max_gap_elev_change_m: Maximum absolute elevation difference to
            consider bridging.
        merge_radius_m: Two nodes within this distance (and similar elev)
            are merged into one during construction.
        merge_elev_m: Maximum elevation difference for merging.
        split_min_dist_m: When splitting a run at intermediate points near
            other nodes, this is the proximity threshold.
        split_min_elev_m: Maximum elevation diff for splitting at a passing node.
    """
    ski_speed_ms: float = 8.0

    lift_time_by_type: dict[str, int] = field(default_factory=lambda: {
        "chair_lift": 360,
        "gondola": 480,
        "cable_car": 420,
        "funicular": 240,
        "drag_lift": 300,
        "t-bar": 300,
        "platter": 300,
        "rope_tow": 120,
        "magic_carpet": 60,
    })
    default_lift_time: int = 300

    gondola_funicular_cablecar_can_descend: bool = True
    lift_down_penalty: int = 600

    skate_cost_flat: int = 30
    skate_cost_moderate: int = 180
    skate_cost_steep: int = 600

    max_gap_distance_m: int = 250
    max_gap_elev_change_m: int = 100

    merge_radius_m: int = 50
    merge_elev_m: int = 20
    split_min_dist_m: int = 50
    split_min_elev_m: int = 20

    connection_cost_multiplier: float = 2.0


@dataclass
class RouteConfig:
    """Tunables for routing.

    Attributes:
        difficulty_penalty: Per-mode dict mapping difficulty → seconds added
            to edge cost. Higher values steer the router away from that
            difficulty under that mode.
        lift_down_easy_discount: When `mode="prefer-easy"`, discount the
            lift-down penalty by this fraction so the router will use a
            gondola down rather than ski a black.
        most_skiing_bonus_per_m: When objective="most-skiing", subtract
            this many seconds per metre of descent on each run edge.
            Higher = router prefers more vertical at the cost of time.
    """
    difficulty_penalty: dict[str, dict[str, int]] = field(default_factory=lambda: {
        "prefer-easy": {
            "novice": 0, "easy": 0, "intermediate": 600, "advanced": 2000,
            "expert": 5000, "freeride": 5000,
        },
        "reds-if-needed": {
            "novice": 0, "easy": 0, "intermediate": 60, "advanced": 800,
            "expert": 3000, "freeride": 3000,
        },
        "any-piste": {
            "novice": 0, "easy": 0, "intermediate": 0, "advanced": 0,
            "expert": 60, "freeride": 120,
        },
    })

    # Should match BuildConfig.lift_down_penalty if you want exact behaviour.
    # Used to back out a fraction of the build-time penalty when mode="prefer-easy",
    # so the router will use a gondola down rather than ski a black.
    lift_down_penalty: int = 600
    lift_down_easy_discount: float = 0.7

    most_skiing_bonus_per_m: float = 0.5


@dataclass
class Destination:
    """A named destination cluster.

    Multi-village resorts (Tignes, Three Valleys) have several base
    villages a skier might want to reach. A Destination defines one
    such target: all graph nodes within `radius_m` of (lat, lon) AND
    within `elev_tolerance_m` above `elev` are tagged as belonging to
    this destination. The router will accept any of them as a goal.

    For single-base resorts you can skip destinations entirely and use
    point-to-point `route()` instead of `route_home()`.
    """
    name: str
    lat: float
    lon: float
    radius_m: float = 500.0
    elev: float | None = None
    elev_tolerance_m: float = 80.0
