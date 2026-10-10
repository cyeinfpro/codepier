"""Official reference scenarios stay distinct from observed model/cache usage."""
import pytest

from shared.token_cost import combined_total, pricing, reference_cost
from shared.token_estimate import summarize, usage


def side(n, **kwargs):
    return dict(state='available', estimated_tokens=n, low=n, high=n,
                characters=n, utf8_bytes=n, source_truncated=False, **kwargs)


def test_official_price_and_direction_mapping():
    value = {'input': side(1_000_000), 'output': side(1_000_000)}
    cost = reference_cost(value)
    assert cost['estimated_usd'] == 51.9
    assert cost['amount_nano_usd'] == 51_900_000_000
    assert cost['model_input_tokens'] == cost['model_output_tokens'] == 1_000_000
    assert cost['estimated_cached_read_tokens'] == 900_000
    assert combined_total(value)['estimated_tokens'] == 2_000_000
    assert cost['actual_usage'] is None
    assert cost['pricing']['cache_write_usage'] is None
    assert cost['pricing']['usd_per_million'] == {'input': 10, 'cached_input': 1, 'cache_write': 12.5, 'output': 50}
    assert cost['pricing']['source_date'] == '2026-10-10'
    assert len(cost['pricing']['sources']) == 2
    assert reference_cost({'input': side(1_000_000)})['estimated_usd'] == 50
    assert reference_cost({'output': side(1_000_000)})['estimated_usd'] == 1.9


@pytest.mark.parametrize('percent,expected', [(0, 10), (1, 9.91), (90, 1.9), (100, 1)])
def test_adjustable_input_cache_scenario(percent, expected):
    cost = reference_cost({'output': side(1_000_000)}, percent)
    assert cost['estimated_usd'] == expected
    assert cost['pricing']['effective_input_usd_per_million'] == expected
    assert cost['partial'] is True  # Missing MCP request is never a measured zero.


@pytest.mark.parametrize('invalid', [-1, 101, 90.1, '90', None, True, float('nan')])
def test_invalid_cache_percentage_fails(invalid):
    with pytest.raises(ValueError):
        pricing(invalid)


def test_unknown_zero_partial_and_extra_actual_usage():
    assert reference_cost({})['amount_nano_usd'] is None
    assert combined_total({})['estimated_tokens'] is None
    value = {'input': side(0), 'output': side(0), 'actual_usage': {'total_tokens': 999999}}
    assert reference_cost(value)['amount_nano_usd'] == 0
    assert reference_cost(value)['actual_usage'] is None
    assert combined_total(value)['estimated_tokens'] == 0
    assert reference_cost(value)['partial'] is False
    value['input']['state'] = 'partial'
    assert reference_cost(value)['partial'] is True


def test_no_long_context_tier_inferred_from_single_mcp_payload():
    cost = reference_cost({'input': side(1_000_000), 'output': side(2_000_000)})
    assert cost['estimated_usd'] == 53.8
    assert cost['pricing']['context'] == 'short_reference'
    assert cost['pricing']['long_context_usd_per_million']['output'] == 75
    assert cost['pricing']['tier_multipliers'] == {'fast': 2}
    assert pricing(reference_model='gpt-6.1-sol')['tier_multipliers'] == {'fast': 2, 'ultrafast': 6}
    assert pricing(reference_model='gpt-6-luna')['tier_multipliers'] == {'fast': 2}


def test_summarize_cost_is_additive_without_per_attempt_rounding():
    rows = [{'id': i, 'operation_id': 'same', 'token_usage': usage(side(1), side(2))} for i in range(5)]
    result = summarize(rows)
    assert result['total']['estimated_tokens'] == 15
    assert result['reference_cost']['amount_nano_usd'] == 5 * (50_000 + 2 * 1900)
    assert result['reference_cost']['estimated_usd'] == .000269
    missing = summarize(rows + [{'id': 99, 'token_usage': None}])
    assert missing['reference_cost']['amount_nano_usd'] == result['reference_cost']['amount_nano_usd']
    assert missing['total']['partial'] is True
    assert missing['input']['unavailable_attempts'] == 1


@pytest.mark.parametrize('model,expected', [('gpt-6-astra', 51.9), ('gpt-6.1-sol', 10.29), ('gpt-6-luna', .519)])
def test_reference_models_are_explicit_and_do_not_change_token_counts(model, expected):
    value = {'input': side(1_000_000), 'output': side(1_000_000)}
    cost = reference_cost(value, reference_model=model)
    assert cost['estimated_usd'] == expected
    assert cost['pricing']['model'] == model
    assert cost['pricing']['provider'] == 'OpenAI'
    assert cost['pricing']['source_date'] == '2026-10-10'
    assert cost['model_input_tokens'] + cost['model_output_tokens'] == 2_000_000
    assert sum(x['amount_pico_usd'] for x in cost['breakdown'].values()) == cost['amount_pico_usd']
    assert cost['breakdown']['cached_input']['estimated_tokens'] == 900_000
    assert cost['breakdown']['uncached_input']['estimated_tokens'] == 100_000
    assert cost['breakdown']['output']['estimated_tokens'] == 1_000_000
    for key in ('actual_usage', 'actual_cache_hit_rate', 'cache_write_tokens', 'reasoning_tokens', 'billing_total'):
        assert cost[key] is None


@pytest.mark.parametrize('model', ['unknown', '', None, [], True])
def test_unknown_reference_model_is_rejected_not_silently_priced_as_astra(model):
    with pytest.raises(ValueError):
        reference_cost({}, reference_model=model)


def test_sub_nano_luna_prices_aggregate_exactly_before_display_rounding():
    rows = [{'id': i, 'token_usage': usage(side(1), side(1))} for i in range(10)]
    total = summarize(rows, cache_read_percent=1, reference_model='gpt-6-luna')['reference_cost']
    costs = [reference_cost(row['token_usage'], 1, 'gpt-6-luna') for row in rows]
    assert total['amount_pico_usd'] == sum(cost['amount_pico_usd'] for cost in costs) == 5_991_000
    assert total['amount_nano_usd'] == 5991
    assert total['model_input_tokens'] == total['model_output_tokens'] == 10


def test_breakdown_preserves_unknown_sides_and_does_not_invent_cache_writes():
    cost = reference_cost({'input': side(10)})
    assert cost['breakdown']['output']['amount_pico_usd'] == 500_000_000
    for key in ('uncached_input', 'cached_input'):
        assert cost['breakdown'][key]['estimated_tokens'] is None
        assert cost['breakdown'][key]['amount_pico_usd'] is None
    assert cost['cache_write_tokens'] is None
