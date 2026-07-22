import pytest

from northgate.proxy_settlement import AttemptTotals
from northgate.usage import UsageResult


@pytest.mark.parametrize(
    ("cached_values", "expected"),
    [
        ([None], None),
        ([0], 0),
        ([20, 30], 50),
        ([20, None], None),
    ],
)
def test_attempt_totals_preserve_cached_prompt_token_completeness(
    cached_values: list[int | None],
    expected: int | None,
) -> None:
    totals = AttemptTotals()
    for cached_prompt_tokens in cached_values:
        totals.add(
            UsageResult(
                prompt_tokens=100,
                completion_tokens=10,
                total_tokens=110,
                cached_prompt_tokens=cached_prompt_tokens,
            ),
            cost_microusd=None,
        )

    usage = totals.usage()
    assert usage.total_tokens == 110 * len(cached_values)
    assert usage.cached_prompt_tokens == expected
