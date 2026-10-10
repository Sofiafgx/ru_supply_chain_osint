"""The requested flat schemas; normalization never supplies primary facts."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from parsers.source_common import date, inn, number, text

FIELDS = {
    1: 'customer_inn customer_name supplier_inn supplier_name contract_id contract_date price_rub subject_text',
    2: 'company_inn source_site record_id record_date cnc_brand cnc_model text_fragment',
    3: 'facility_name facility_inn verification_date equipment_type equipment_modification equipment_serial_number verification_result vri_id',
    4: 'hs_code importer_inn importer_name exporter_name declaration_date net_weight_kg units_qty customs_value_usd',
    5: 'patent_number assignee_name publication_date device_title detected_architecture_channels calculated_component_multiplier raw_snippet_quote',
    6: 'facility_coords t_event t_recovery damage_type scene_id confidence_flags',
    7: 'company_inn record_id record_date supervisory_authority production_shop_type equipment_mention raw_quote',
}
FIELDS = {block: fields.split() for block, fields in FIELDS.items()}
DATES = {'contract_date', 'record_date', 'verification_date', 'declaration_date', 'publication_date'}
NUMBERS = {'price_rub', 'net_weight_kg', 'units_qty', 'customs_value_usd', 'calculated_component_multiplier'}
QUOTES = {'subject_text', 'text_fragment', 'raw_snippet_quote', 'raw_quote'}


def utc(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None


def normalize(block, data):
    row = {}
    warnings = list(data.get('warnings', []))
    for field in FIELDS[block]:
        value = data.get(field)
        if field.endswith('_inn'):
            result = inn(value)
        elif field in DATES:
            result = date(value)
        elif field in NUMBERS:
            result = number(value) if not isinstance(value, (dict, list, bool)) else None
        elif field in ('t_event', 't_recovery'):
            result = utc(value)
        elif field == 'hs_code':
            result = text(value)
            if result and not re.fullmatch(r'\d{4,6}', result):
                result = None
        elif field == 'detected_architecture_channels':
            result = int(value) if isinstance(value, (int, str)) and not isinstance(value, bool) and re.fullmatch(r'[1-9]\d*', str(value)) else None
        elif field == 'facility_coords':
            result = None
            if isinstance(value, (list, tuple)) and len(value) == 2:
                try:
                    lat, lon = map(float, value)
                    if all(math.isfinite(v) for v in (lat, lon)) and -90 <= lat <= 90 and -180 <= lon <= 180:
                        result = [lat, lon]
                except (ValueError, TypeError):
                    pass
        elif field == 'confidence_flags':
            result = value if isinstance(value, dict) else None
        elif field in QUOTES:
            result = value if isinstance(value, str) and value.strip() else None
        else:
            result = text(value) if not isinstance(value, (dict, list)) else None
        if value is not None and result is None:
            warnings.append('invalid_or_insufficient_' + field)
        row[field] = result
    # Additional evidence/interpretation stays explicit, outside the primary fields.
    row.update({k: v for k, v in data.items() if k not in FIELDS[block] and k != 'warnings'})
    row['warnings'] = sorted(set(warnings))
    return row
