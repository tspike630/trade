# PR 202 deploy bundle

Merge `dc5af7f208a16e9fe6a7e5aa1eca6cba437ea314`. Head `ade9d1444f9990ae02588d8bd4cc564d58a15cab`. Base `450d22928ddbc6168f6a1df01e09781e85e10336`.

Archive: `pr202-deploy-ade9d144.tar`
sha256: `be450d09ecf465fa5360df289a448dad5474591ba2f8c1ceda394401cdd2d505`
SHA256SUMS file inside the archive: `7939a4f0a1d847ecdce6a451341d893c5d681d1592e3d0309eda707ec7c90ce3`

Nine code files, plus `DEPLOY.txt` and `SHA256SUMS`. No database, no secret, no other pull request.

| sha256 | bytes | path |
| :--- | ---: | :--- |
| `da32c258c34c7d96c6157045fc48a260ae2652e8daadf8b2861e61c79fbf6721` | 6012 | `crawler/NEWS_SIDECHANNEL.md` |
| `34d9fdc11116e31e274e3e829cec133d8d75786c1fd8822e61236ec6a4f1ae72` | 17298 | `crawler/sources/news.py` |
| `d3f7687ece3a88b45e40af53470f9b6cbaa32e1a8ce97d0bd58b0974b792485d` | 20750 | `decision_engine/core/engine.py` |
| `37b1c03423a889652542eddf312ef45674d7b9abbb68f2aeb4f85afe0d04c073` | 37736 | `decision_engine/core/news_shock_audit.py` |
| `a167cb0da71d141b71b9a2237c18bdb926cd3a8fccd4cbffbec8c4e686840ed6` | 40782 | `decision_engine/core/news_shock_gate.py` |
| `99a250f741af3977b7341011112fda6b8ef4e4975603a9efd2bb3ba8c26c0f22` | 24212 | `decision_engine/core/regime_strategy_router.py` |
| `99488855b4db1b8cbafb2d98ef1a65e811b9de43c23c762fa975b6ba69a2175c` | 42616 | `decision_engine/tests/test_news_shock_gate.py` |
| `29c43792cd73aad0969ebbb4f7280c81fa9b06e1bdbd57409ba51351428cc7b3` | 2443 | `DEPLOY.txt` |
| `da32c258c34c7d96c6157045fc48a260ae2652e8daadf8b2861e61c79fbf6721` | 6012 | `docs/NEWS_SIDECHANNEL.md` |
| `34d9fdc11116e31e274e3e829cec133d8d75786c1fd8822e61236ec6a4f1ae72` | 17298 | `sources/news.py` |

`sources/news.py` matches `crawler/sources/news.py`. The two sidechannel docs match.

Dependencies already on the host: Python 3.12 stdlib for the gate, audit, and router. `httpx>=0.27.0` only for `python -m sources.news`. `crawler/logging_setup.py` is not in the increment.

`apply_five_state_regime_router(candidates, hot_rows, macro_halt=...)` still works. `news_snapshot`, `onchain_calibration`, and `aligned_rank_weight` default to `None`. `None` does not change rank or size.

Fed RSS uses the existing `python -m sources.news --once` with no `--sources` filter. Do not add a second timer.

Read-only smoke:

```bash
PYTHONPATH=crawler python3 -m sources.news --list-sources
PYTHONPATH=. python3 -m unittest decision_engine.tests.test_news_shock_gate
```

`--list-sources` does not open a database. Do not start a trader.

Rollback is the nine paths at base `450d22928ddbc6168f6a1df01e09781e85e10336`. See `DEPLOY.txt` in the archive. That does not touch databases.
