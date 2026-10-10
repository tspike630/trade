"""Quick validation unit checks (no network)."""

from validate import validate_pair, validate_token_profile

ok, err = validate_token_profile({"chainId": "solana", "tokenAddress": "Abc"})
assert ok and err is None, err
bad, err = validate_token_profile({"chainId": "", "tokenAddress": "x"})
assert bad is None and err
bad2, err2 = validate_token_profile("nope")
assert bad2 is None

pair_ok, perr = validate_pair(
    {
        "chainId": "ethereum",
        "pairAddress": "0xABC",
        "baseToken": {"address": "0xdef"},
        "priceUsd": "1.25",
        "volume": {"h24": "100"},
    }
)
assert pair_ok and pair_ok["priceUsd"] == 1.25 and pair_ok["volume"]["h24"] == 100.0
pair_bad, _ = validate_pair({"chainId": "ethereum", "pairAddress": "0x1"})
assert pair_bad is None
print("VALIDATE_DEMO_OK")
