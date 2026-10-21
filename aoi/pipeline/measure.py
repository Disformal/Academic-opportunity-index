"""Decision-point measurement functions.

Pure functions for computing metrics used at the Phase 1 → Phase 2 gate.
No database access — takes data as arguments.
"""

import math


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """Wilson score lower bound at given confidence level.

    Returns the lower bound of the Wilson score confidence interval for a
    Bernoulli parameter. Default *z* = 1.96 gives 95 % confidence.
    """
    if total == 0:
        return 0.0

    p = successes / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return (centre - spread) / denominator


def szymkiewicz_simpson(set_a: set[str], set_b: set[str]) -> float:
    """Szymkiewicz-Simpson overlap coefficient.

    Returns |A & B| / min(|A|, |B|).  1.0 when one set is a subset of the
    other, 0.0 when disjoint.
    """
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / min(len(set_a), len(set_b))
