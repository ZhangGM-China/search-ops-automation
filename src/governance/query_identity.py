import re
import unicodedata


def normalize_query_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def normalize_query_key(value: object) -> str:
    return normalize_query_text(value).casefold()


_ATLAS_GOVERNANCE_SUFFIXES = ("女士", "男士", "儿童", "家居")


def is_non_intent_query(value: object) -> bool:
    """Return whether a query is a product-code-like token, not a search intent."""
    text = normalize_query_text(value)
    if not text:
        return True
    # Atlas appends a demographic suffix during extraction; it must not hide a code.
    core = re.sub(r"\s+(?:女士|男士|儿童|家居|男|女|童)$", "", text).strip()
    if re.fullmatch(r"\d{4,}", core):
        return True
    if re.fullmatch(r"\d{4,}(?:\s*/\s*\d{3,})+", core):
        return True
    # Long numeric/alphanumeric SKU tokens are not intent-bearing queries.
    return bool(re.fullmatch(r"(?:[A-Za-z]{1,5}[-_ ]?)?\d{6,}[A-Za-z0-9_-]*", core))


def transform_governance_payload_query(brand: object, value: object) -> str:
    text = normalize_query_text(value)
    if normalize_query_key(brand) == "atlas":
        for suffix in _ATLAS_GOVERNANCE_SUFFIXES:
            text = text.replace(f" {suffix}", "")
        text = normalize_query_text(text)
    return text


def normalize_governance_query_key(brand: object, value: object) -> str:
    return transform_governance_payload_query(brand, value).casefold()
