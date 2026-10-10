"""Evidence-gated metrics. No default stock, consumption, or recovery periods."""
import math
from datetime import datetime


def nonnegative(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Expected finite nonnegative number')
    return value


def tts(stock, daily_demand, stock_unit, demand_unit, verified=False):
    if not verified or stock is None or daily_demand is None or not stock_unit or stock_unit != demand_unit:
        return None
    nonnegative(stock); nonnegative(daily_demand)
    return stock / daily_demand if daily_demand > 0 else None


def loss_scenario(duration_days, daily_loss, max_loss, unit, assumption):
    """Caller supplies all scenario assumptions and compatible loss units."""
    if not unit or not assumption or len(duration_days) != 2:
        raise ValueError('Scenario requires units, assumptions and two duration bounds')
    lo, hi = map(nonnegative, duration_days)
    if lo > hi:
        raise ValueError('Reversed duration bounds')
    if daily_loss is None:
        loss = rei = None
    else:
        nonnegative(daily_loss)
        loss = [lo * daily_loss, hi * daily_loss]
        if max_loss is None:
            rei = None
        else:
            nonnegative(max_loss)
            rei = [v / max_loss for v in loss] if max_loss > 0 else None
    return dict(evidence_class='scenario', duration_days=[lo, hi], loss_interval=loss,
                loss_unit=unit, rei_interval=rei, assumption=assumption)


def radar_candidate(previous, observed):
    if previous is None or observed is None:
        return None
    a, b = [datetime.fromisoformat(v.replace('Z', '+00:00')) for v in (previous, observed)]
    if a.tzinfo is None or b.tzinfo is None or a >= b:
        raise ValueError('Radar interval requires ordered timezone-aware dates')
    return dict(metric='ttr_radar_candidate', t_prev=previous, t_observed=observed,
                evidence_class='observation_candidate', confirms_production_recovery=False)


def myerson(players, edges, value):
    """Exact Shapley value of the graph-restricted game; v(empty)=0.

    value receives a frozenset component. No production value function is inferred.
    """
    players = tuple(sorted(set(players)))
    if len(players) > 20:
        raise ValueError('Exact calculation limited to 20 players')
    if value(frozenset()) != 0:
        raise ValueError('v(empty) must be zero')
    index = {p: i for i, p in enumerate(players)}
    adjacency = [0] * len(players)
    for a, b in edges:
        if a not in index or b not in index or a == b:
            raise ValueError('Edge outside player scope or self-edge')
        adjacency[index[a]] |= 1 << index[b]
        adjacency[index[b]] |= 1 << index[a]
    component_values = {}
    restricted = [0.0] * (1 << len(players))
    for mask in range(1, len(restricted)):
        unseen = mask
        while unseen:
            frontier = unseen & -unseen
            component = 0
            while frontier:
                bit = frontier & -frontier
                frontier ^= bit
                component |= bit
                unseen &= ~bit
                neighbors = adjacency[bit.bit_length() - 1] & unseen & ~component
                frontier |= neighbors
            if component not in component_values:
                result = value(frozenset(p for i, p in enumerate(players) if component & (1 << i)))
                if isinstance(result, bool) or not math.isfinite(result):
                    raise ValueError('Coalition values must be finite')
                component_values[component] = result
            restricted[mask] += component_values[component]
    n = len(players)
    result = dict.fromkeys(players, 0.0)
    for i, p in enumerate(players):
        bit = 1 << i
        for mask in range(len(restricted)):
            if not mask & bit:
                k = bin(mask).count('1')
                result[p] += (restricted[mask | bit] - restricted[mask]) / (n * math.comb(n - 1, k))
    return result
