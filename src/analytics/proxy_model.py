"""Compatibility facade for evidence-gated resilience metrics."""
import networkx as nx
from analytics.resilience import tts, nonnegative
from data_sanitization import sanitize_record, field_evidence
from eis_ftp_contracts import valid_inn


class SupplyChainProxyModel:
    def __init__(self, base_monthly_capacity=None, capacity_evidence=None):
        proof = {'field_evidence': {'base_monthly_capacity': capacity_evidence}}
        self.base_capacity = base_monthly_capacity if field_evidence(proof, 'base_monthly_capacity', base_monthly_capacity) else None
        self.graph = nx.MultiDiGraph()

    def build_network(self, edges):
        self.graph.clear()
        for e in edges:
            if not isinstance(e, dict):
                continue  # Historical TradeEdge has no contract/case evidence.
            e, _, reasons = sanitize_record(e)
            if reasons:
                continue
            kind = e.get('kind')
            identifier = e.get('contract_id') if kind == 'contract' else e.get('case_number') if kind == 'court' else None
            a, b = (e.get('customer_inn'), e.get('supplier_inn')) if kind == 'contract' else (e.get('plaintiff_inn'), e.get('respondent_inn'))
            if e.get('verified') is True and e.get('source_url') and e.get('input_sha256') and e.get('collected_at') and identifier and a and b and a != b and valid_inn(a) and valid_inn(b):
                self.graph.add_edge(a, b, key=(kind, identifier), **e)

    def compute_ttr(self, inn, tenders=None, visual_ttr=None):
        return None  # Radar or procurement evidence alone cannot establish operational TTR.

    def compute_tts(self, inn, stock=None, daily_demand=None, stock_unit=None, demand_unit=None, verified=False):
        return tts(stock, daily_demand, stock_unit, demand_unit, verified)

    def compute_coalition_value(self, coalition, critical_nodes):
        if self.base_capacity is None:
            return None
        nonnegative(self.base_capacity)
        factor = 1.0
        for node, penalty in critical_nodes.items():
            if not 0 <= nonnegative(penalty) <= 1:
                raise ValueError('Scenario factors must be between zero and one')
            if node not in coalition:
                factor *= penalty
        return self.base_capacity * factor

    def compute_rei(self, ttr_months, monthly_loss, max_loss):
        if any(v is None for v in (ttr_months, monthly_loss, max_loss)):
            return None
        for v in (ttr_months, monthly_loss, max_loss):
            nonnegative(v)
        return ttr_months * monthly_loss / max_loss if max_loss > 0 else None
