import pytest

from backend.entity_resolver import EntityResolver


@pytest.fixture
def resolver():
    return EntityResolver()


@pytest.mark.parametrize(
    ("message", "symbol", "market"),
    [
        ("研究腾讯近三年盈利", "0700.HK", "HK"),
        ("分析贵州茅台估值", "600519.SH", "CN"),
        ("看看 NVDA.US 的风险", "NVDA.US", "US"),
    ],
)
def test_resolves_unique_alias_or_exact_symbol(resolver, message, symbol, market):
    result = resolver.resolve(message)
    assert result.status == "resolved"
    assert result.selected.symbol == symbol
    assert result.selected.market == market


@pytest.mark.parametrize("message", ["研究阿里巴巴", "分析比亚迪现金流"])
def test_multiple_listings_require_confirmation(resolver, message):
    result = resolver.resolve(message)
    assert result.status == "ambiguous"
    assert len(result.candidates) == 2
    assert result.selected is None
    assert "USER_CONFIRMATION_REQUIRED" in result.reason_codes


def test_current_case_reference_uses_confirmed_security(resolver):
    result = resolver.resolve(
        "再看看该公司的分红",
        current_company="紫金矿业",
        current_symbol="601899.SH",
        current_market="CN",
    )
    assert result.status == "resolved"
    assert result.selected.company == "紫金矿业"
    assert result.reason_codes == ["CURRENT_CASE_REFERENCE"]


def test_unknown_company_fails_closed(resolver):
    result = resolver.resolve("研究一个完全未知的对象")
    assert result.status == "unresolved"
    assert result.candidates == []
    assert result.selected is None


def test_catalog_is_not_fuzzy_enough_to_guess_typo(resolver):
    result = resolver.resolve("研究阿里爸爸")
    assert result.status == "unresolved"
