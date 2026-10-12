"""Compute methodology 1.1 metrics from the declared local contract snapshot."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import date
import hashlib
import json
import logging
import math
from pathlib import Path

import networkx as nx
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from supplier_enrichment import valid_inn


ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")


def validate_export(records, schema_path, export_schema_path):
    """Resolve both local schemas once and validate the complete array."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    export_schema = json.loads(export_schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator.check_schema(export_schema)
    export_schema.setdefault("$id", export_schema_path.resolve().as_uri())
    resource = Resource.from_contents(schema)
    registry = Registry().with_resources([
        (schema_path.resolve().as_uri(), resource),
        (schema["$id"], resource),
        (export_schema["$id"], Resource.from_contents(export_schema)),
    ]).crawl()
    Draft202012Validator(export_schema, registry=registry,
                         format_checker=FormatChecker()).validate(records)


def load_contracts(path, snapshot, decay):
    """Keep unknown weights separate from their zero *known contribution*."""
    csv.field_size_limit(20_000_000)
    graph = nx.DiGraph()
    names, evidence = {}, {}
    counts = Counter()
    years = Counter()
    seen = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"law", "contract_number", "customer_inn", "supplier_inns",
                    "sign_date", "price_rub"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Missing contract columns")
        for number, row in enumerate(reader, 1):
            counts["rows"] += 1
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed CSV record {number}")
            customer = row["customer_inn"].strip()
            suppliers = json.loads(row["supplier_inns"] or "[]")
            if not suppliers:
                counts["missing_supplier_rows"] += 1
                continue
            if (not isinstance(suppliers, list) or len(suppliers) != 1
                    or not valid_inn(customer) or not valid_inn(suppliers[0])):
                raise ValueError(f"Ambiguous or invalid parties at record {number}")
            supplier = suppliers[0]
            key = (row["law"], row["contract_number"])
            if not all(key):
                raise ValueError(f"Missing contract identity at record {number}")
            if key in seen:
                raise ValueError(f"Duplicate contract {key}; revision selection required")
            seen.add(key)
            counts["valid_bilateral_rows"] += 1
            ref = f"{path.resolve()}#csv_record={number}"
            for inn, name in ((customer, row.get("customer_name", "")),):
                if name.strip():
                    names.setdefault(inn, name.strip())
            supplier_names = json.loads(row.get("supplier_names") or "[]")
            if (isinstance(supplier_names, list) and len(supplier_names) == 1
                    and isinstance(supplier_names[0], str)
                    and supplier_names[0].strip()):
                names.setdefault(supplier, supplier_names[0].strip())
            for inn in (supplier, customer):
                evidence.setdefault(inn, ref)
            graph.add_nodes_from((supplier, customer))
            if supplier == customer:
                counts["self_relationship_rows"] += 1
                continue
            unknown = []
            try:
                signed = date.fromisoformat(row["sign_date"].strip())
            except ValueError:
                signed = None
                unknown.append("unknown_sign_date")
                counts["unknown_sign_date_rows"] += 1
            if signed and (signed > snapshot or signed < date(2022, 1, 1)):
                counts["outside_period_rows"] += 1
                continue
            if signed:
                years[str(signed.year)] += 1
            text = row["price_rub"].strip()
            if not text:
                price = None
                unknown.append("unknown_price")
                counts["unknown_price_rows"] += 1
            else:
                price = float(text)
                if not math.isfinite(price) or price < 0:
                    raise ValueError(f"Invalid price at record {number}")
                if price == 0:
                    counts["zero_price_rows"] += 1
            if row.get("currency_original") != "RUB":
                counts["non_rub_or_unknown_original_currency_rows"] += 1
            weight = (math.log1p(price) * decay ** (snapshot.year - signed.year)
                      if price is not None and signed is not None else 0.0)
            if not graph.has_edge(supplier, customer):
                graph.add_edge(supplier, customer, weight=0.0,
                               weight_complete=True, contract_refs=[],
                               missing_reasons=[])
            edge = graph[supplier][customer]
            edge["weight"] += weight
            edge["weight_complete"] &= not unknown
            edge["contract_refs"].append({
                "law": key[0], "contract_number": key[1],
                "csv_record": number, "source_url": row.get("source_url") or None,
            })
            edge["missing_reasons"] = sorted(set(edge["missing_reasons"] + unknown))
    return graph, names, evidence, dict(counts), dict(sorted(years.items()))


def positive_graph(observed):
    graph = nx.DiGraph()
    graph.add_nodes_from(sorted(observed))
    for u, v, data in sorted(observed.edges(data=True)):
        if data["weight"] > 0:
            graph.add_edge(u, v, weight=data["weight"],
                           distance=1.0 / data["weight"])
    return graph


def efficiency_vulnerability(graph):
    """Exact fixed-n efficiency loss; endpoint-only nodes need no rerouting.

    A source or sink cannot be an internal node of a positive-length path.
    Its loss is the sum of the baseline efficiencies of its endpoint pairs.
    For transit nodes, only sources that can reach them need recalculation.
    Disjoint weak components cancel in the difference.
    """
    total_terms, losses = [], {}
    transit_count = sum(bool(graph.in_degree(n) and graph.out_degree(n))
                        for n in graph)
    LOG.info("Exact efficiency: %s nodes, %s transit nodes", len(graph), transit_count)
    for component in nx.weakly_connected_components(graph):
        subgraph = graph.subgraph(component)
        baseline, outgoing = {}, {}
        incoming = defaultdict(float)
        for source in sorted(component):
            if not subgraph.out_degree(source):
                outgoing[source] = 0.0
                continue
            lengths = nx.single_source_dijkstra_path_length(
                subgraph, source, weight="distance")
            baseline[source] = lengths
            terms = [1.0 / length for target, length in lengths.items()
                     if target != source]
            outgoing[source] = math.fsum(terms)
            for target, length in lengths.items():
                if target != source:
                    incoming[target] += 1.0 / length
        total_terms.append(math.fsum(outgoing.values()))
        for node in sorted(component):
            endpoint_loss = outgoing[node] + incoming[node]
            if not (subgraph.in_degree(node) and subgraph.out_degree(node)):
                losses[node] = endpoint_loss
                continue
            # Keep every node, including the disconnected node itself.
            disabled = nx.subgraph_view(
                subgraph, filter_edge=lambda u, v, node=node: u != node and v != node)
            rerouting_losses = []
            for source in nx.ancestors(subgraph, node):
                before = baseline[source]
                after = nx.single_source_dijkstra_path_length(
                    disabled, source, weight="distance")
                for target, length in before.items():
                    if target in (source, node):
                        continue
                    old = 1.0 / length
                    new_length = after.get(target)
                    new = 1.0 / new_length if new_length is not None else 0.0
                    difference = old - new
                    if difference < -1e-10 * max(1.0, old):
                        raise ArithmeticError("Removing edges improved a shortest path")
                    rerouting_losses.append(max(0.0, difference))
            losses[node] = endpoint_loss + math.fsum(rerouting_losses)
    total = math.fsum(total_terms)
    n = len(graph)
    efficiency = total / (n * (n - 1)) if n > 1 else 0.0
    if total == 0:
        return efficiency, {node: None for node in graph}
    vulnerability = {}
    for node in graph:
        value = losses[node] / total
        if not -1e-10 <= value <= 1.0 + 1e-10:
            raise ArithmeticError(f"Vulnerability outside its mathematical range: {value}")
        vulnerability[node] = min(1.0, max(0.0, value))
    return efficiency, vulnerability


def supplier_shares(observed):
    shares, hhi, incomplete, zero_inflow = {}, {}, set(), set()
    for customer in observed:
        edges = list(observed.in_edges(customer, data=True))
        if any(not data["weight_complete"] for _, _, data in edges):
            incomplete.add(customer)
            hhi[customer] = None
            continue
        total = math.fsum(data["weight"] for _, _, data in edges)
        if not total:
            zero_inflow.add(customer)
            hhi[customer] = None
            continue
        fractions = []
        for supplier, _, data in edges:
            share = data["weight"] / total
            shares[supplier, customer] = share
            fractions.append(share)
        hhi[customer] = math.fsum(value * value for value in fractions)
    ssr, status = {}, {}
    for supplier in observed:
        customers = set(observed.successors(supplier))
        if customers & incomplete:
            ssr[supplier], status[supplier] = None, "incomplete_weights"
        elif customers & zero_inflow:
            ssr[supplier], status[supplier] = None, "no_positive_inflow"
        elif not customers:
            ssr[supplier], status[supplier] = 0.0, "no_observed_customers"
        else:
            value = max(shares[supplier, customer] for customer in customers)
            ssr[supplier] = value
            status[supplier] = ("dominant_observed_weight_share" if value >= 0.99
                                else "observed_weight_share")
    return ssr, hhi, status, sorted(incomplete)


def risk_value(flags):
    weights = (0.4, 0.4, 0.2)
    values = [flags[name] for name in (
        "is_restricted", "critical_component_flow", "has_active_supply_disputes")]
    lower = math.fsum(weight * value for weight, value in zip(weights, values)
                      if value is not None)
    upper = lower + math.fsum(weight for weight, value in zip(weights, values)
                             if value is None)
    return (lower if all(value is not None for value in values) else None,
            [lower, upper])


def normalize(values):
    if not values or any(value is None for value in values.values()):
        return {key: None for key in values}, {
            "minimum": None, "maximum": None, "status": "unavailable"}
    low, high = min(values.values()), max(values.values())
    if low == high:
        return {key: 0.0 for key in values}, {
            "minimum": low, "maximum": high, "status": "degenerate_range"}
    return {key: (value - low) / (high - low) for key, value in values.items()}, {
        "minimum": low, "maximum": high, "status": "computed"}


def court_audit(path):
    counts, keys, cases = Counter(), set(), set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            data = json.loads(line).get("data", {})
            keys.update(data)
            counts["rows"] += 1
            if data.get("case_number"):
                cases.add(data["case_number"].strip())
            for name in ("plaintiff_inn", "respondent_inn",
                         "has_active_supply_disputes", "case_status", "dispute_subject"):
                counts[f"{name}_populated"] += data.get(name) is not None
    return {"counts": dict(counts), "unique_case_numbers": len(cases),
            "data_fields": sorted(keys),
            "risk_evidence_usable": False,
            "reason": "No verified party identifiers, supply-dispute subject and active status"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contracts", type=Path,
                        default=ROOT / "data/collection_20261008/exports/contracts.csv")
    parser.add_argument("--profiles", type=Path,
                        default=ROOT / "config/russian_defence_entities.json")
    parser.add_argument("--courts", type=Path,
                        default=ROOT / "data/compliance_offline_20261009/browser_cases.jsonl")
    parser.add_argument("--snapshot-date", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--decay", type=float, default=0.5436890126920764)
    parser.add_argument("--betweenness-k", type=int, default=512,
                        help="Number of sampled sources; 0 computes exact betweenness")
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--output", type=Path, default=ROOT / "leaderboard_cci_1.1.json")
    parser.add_argument("--report-dir", type=Path,
                        default=ROOT / "reports/cci_1_1_20261011")
    args = parser.parse_args()
    if not 0 < args.decay <= 1 or args.betweenness_k < 0:
        parser.error("Require 0 < decay <= 1 and betweenness-k >= 0")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    args.report_dir.mkdir(parents=True, exist_ok=True)
    schema_path = ROOT / "schemas/cci_assessment.schema.json"
    export_schema_path = ROOT / "schemas/leaderboard_cci_1.1.schema.json"
    methodology_path = ROOT / "docs/CCI_METHODOLOGY_UK.md"
    validation_path = ROOT / "reports/graph_aggregate_audit_20261010/validation.json"
    sources = [args.contracts, args.profiles, args.courts, schema_path, export_schema_path,
               methodology_path, validation_path, Path(__file__)]
    source_hashes = {str(path.resolve()): sha256(path) for path in sources}
    LOG.info("Reading and auditing contract records")
    observed, names, evidence, counts, years = load_contracts(
        args.contracts, args.snapshot_date, args.decay)
    profiles = json.loads(args.profiles.read_text(encoding="utf-8"))["entities"]
    by_inn = {}
    for profile in profiles:
        inn = profile["inn"]
        if not valid_inn(inn) or inn in by_inn:
            raise ValueError("Invalid or repeated profile identifier")
        by_inn[inn] = profile
        observed.add_node(inn)
        names[inn] = profile["name"]
    graph = positive_graph(observed)
    graph_payload = {"nodes": sorted(graph), "edges": [
        [u, v, data["weight"].hex()] for u, v, data in sorted(graph.edges(data=True))]}
    graph_fingerprint = hashlib.sha256(
        json.dumps(graph_payload, sort_keys=True).encode()).hexdigest()
    cohort_id = "all_bilateral_participants_and_profiles:" + hashlib.sha256(
        json.dumps(sorted(graph)).encode()).hexdigest()
    efficiency, vulnerability = efficiency_vulnerability(graph)
    k = min(args.betweenness_k, len(graph)) if args.betweenness_k else None
    LOG.info("Betweenness: %s, k=%s, seed=%s",
             "sampled" if k and k < len(graph) else "exact", k, args.seed)
    betweenness = nx.betweenness_centrality(
        graph, k=k, normalized=True, endpoints=False, weight="distance", seed=args.seed)
    LOG.info("Computing shares, observed cut vertices and normalization")
    ssr, hhi, dependency_status, incomplete = supplier_shares(observed)
    cuts = set(nx.articulation_points(observed.to_undirected()))
    norm_v, v_range = normalize(vulnerability)
    norm_b, b_range = normalize(betweenness)
    court_summary = court_audit(args.courts)
    manifest_path = (args.report_dir / "manifest.json").resolve()
    records, graph_ids = [], {}
    for inn in sorted(graph):
        profile = by_inn.get(inn, {})
        entity_id = profile.get("entity_id") or f"inn:{inn}"
        graph_ids[entity_id] = inn
        # These specific input files contain no verified values for these flags.
        # Search participation, an HS prefix or a source URL cannot supply them.
        flags = {name: None for name in (
            "is_restricted", "critical_component_flow", "has_active_supply_disputes")}
        risk, bounds = risk_value(flags)
        components = {"normalized_v_eff_latora": norm_v[inn],
                      "normalized_betweenness": norm_b[inn],
                      "ssr_weight_share": ssr[inn], "regulatory_risk": risk}
        missing = [f"unavailable:{name}" for name, value in components.items()
                   if value is None]
        missing.extend(f"unverified:{name}" for name, value in flags.items()
                       if value is None)
        if ssr[inn] is None:
            missing.append(dependency_status[inn])
        score = (math.fsum(weight * value for weight, value in zip(
            (0.35, 0.25, 0.20, 0.20), components.values())) if not missing else None)
        refs = [evidence[inn]] if inn in evidence else []
        if profile:
            refs.append(f"{args.profiles.resolve()}#entity_id={entity_id}")
        records.append({
            "methodology_version": "1.1", "entity_id": entity_id,
            "entity_name": names.get(inn) or f"Підприємство з ІПН {inn}",
            "tier_estimate": None, "snapshot_date": args.snapshot_date.isoformat(),
            "cohort_id": cohort_id, "run_manifest_ref": str(manifest_path),
            "calculation_status": "computed" if score is not None else "insufficient_data",
            "metrics": {"v_eff_latora": vulnerability[inn],
                        "betweenness": betweenness[inn], "ssr_weight_share": ssr[inn],
                        "hhi_customer_weight_share": hhi[inn],
                        "regulatory_risk": risk, "regulatory_risk_bounds": bounds},
            "normalized_metrics": {"v_eff_latora": norm_v[inn], "betweenness": norm_b[inn]},
            "composite_criticality_index": score,
            "dependency_profile": {
                "is_cut_vertex_observed": inn in cuts,
                "dominant_observed_weight_share": (
                    ssr[inn] >= 0.99 if ssr[inn] is not None else None),
                "dependency_status": dependency_status[inn]},
            "resilience_parameters": {
                "units_per_system": None, "tts_days": None, "ttr_days": None,
                "ttr_scenario_months": None},
            "risk_flags": flags, "evidence_refs": refs, "missing_reasons": missing,
        })
    records.sort(key=lambda record: (
        record["composite_criticality_index"] is None,
        -(record["composite_criticality_index"] or 0.0), record["entity_id"]))
    LOG.info("Validating all %s exported records against both local schemas", len(records))
    validate_export(records, schema_path, export_schema_path)
    manifest = {
        "methodology_version": "1.1", "snapshot_date": args.snapshot_date.isoformat(),
        "timezone": "Europe/Kiev", "source_sha256": source_hashes,
        "period_from": "2022-01-01", "period_to": args.snapshot_date.isoformat(),
        "base_year": args.snapshot_date.year, "decay_lambda": args.decay,
        "decay_basis": "User-selected formula; validation.json is an earlier issue report",
        "annual_weights": {str(year): args.decay ** (args.snapshot_date.year - year)
                           for year in range(2022, args.snapshot_date.year + 1)},
        "value_reference": 1.0, "currency": "RUB", "value_basis": "nominal price_rub",
        "currency_conversion_independently_verified": False,
        "relationship_verification_basis": (
            "User declares bilateral records confirmed; this run checks identifiers, "
            "dates and values locally, not registry identity or performance"),
        "contracts": counts, "bilateral_contract_years": years,
        "cohort_id": cohort_id, "cohort_definition": "All valid bilateral participants plus profiles",
        "entity_id_to_graph_id": graph_ids, "graph_fingerprint": graph_fingerprint,
        "nodes": len(graph), "observed_directed_edges": observed.number_of_edges(),
        "positive_directed_edges": graph.number_of_edges(),
        "incomplete_pair_weights": sum(not data["weight_complete"]
                                       for _, _, data in observed.edges(data=True)),
        "customer_incomplete_weight_count": len(incomplete),
        "profile_count": len(profiles), "global_efficiency": efficiency,
        "vulnerability_method": "Exact fixed-node-count edge deactivation; component-local rerouting",
        "betweenness": {"method": "sampled" if k and k < len(graph) else "exact",
                        "k": k, "seed": args.seed, "normalized": True,
                        "endpoints": False, "weight": "distance"},
        "normalization": {"v_eff_latora": v_range, "betweenness": b_range},
        "networkx_version": nx.__version__, "cut_vertex_graph": "observed_undirected",
        "cut_vertex_count": len(cuts), "court_audit": court_summary,
        "risk_flags_evidence_status": "No verified flags in allowed input files",
        "cci_weights": [0.35, 0.25, 0.20, 0.20],
        "numeric_cci_count": sum(record["composite_criticality_index"] is not None
                                 for record in records),
        "null_cci_count": sum(record["composite_criticality_index"] is None
                              for record in records),
        "validated_record_count": len(records), "record_schema": str(schema_path.resolve()),
        "export_schema": str(export_schema_path.resolve()),
        "export_validation": "Complete array; Draft 2020-12 with a pre-crawled local registry",
        "output": str(args.output.resolve()),
    }
    for path in sources:
        if sha256(path) != source_hashes[str(path.resolve())]:
            raise RuntimeError(f"Source changed during calculation: {path}")
    write_json(manifest_path, manifest)
    with (args.report_dir / "edge_provenance.jsonl").open("w", encoding="utf-8") as handle:
        for u, v, data in sorted(observed.edges(data=True)):
            handle.write(json.dumps({"supplier_inn": u, "customer_inn": v, **data},
                                    ensure_ascii=False, allow_nan=False) + "\n")
    with (args.report_dir / "cut_vertices.csv").open(
            "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["entity_id", "inn", "entity_name"])
        for record in records:
            if record["dependency_profile"]["is_cut_vertex_observed"]:
                writer.writerow([record["entity_id"], graph_ids[record["entity_id"]],
                                 record["entity_name"]])
    write_json(args.output, records)
    manifest["output_sha256"] = sha256(args.output)
    write_json(manifest_path, manifest)
    LOG.info("Validated %s records; numerical CCI: %s; report: %s",
             len(records), manifest["numeric_cci_count"], manifest_path)


if __name__ == "__main__":
    main()
