# t346 本轮：OCO 错误码不再授权买回

父提交（远端核到未变后才改）：`4ceb57d7ee86e29302de8ad7f085898b046caaa5`  
分支：`cursor/cross-margin-track-9967`  
PR：https://github.com/tspike630/trade/pull/222

本说明只记录这一轮。没有交易所实证，没有真实账户调用，没有批准杠杆倍数或保证金阈值。

## 改了什么

`MarginExchangeError.is_deterministic_pre_exec_oco_reject` 不再用整数错误码返回真。`-2010` 既可以是余额/过滤器文字，也可以是重复客户号；重复客户号可能表示列表已经落地。空的 `openOrders` 加未锁定余额不能证明 POST 没落账，因此这条自动买回路径关掉，而不是补一组“官方已证实”的码。

`new_oco` 抛出的任何 `MarginExchangeError`（含重复文案、允许码配矛盾文案、空消息、未知码、5xx、429）都把该 OCO 账本键占成 `unknown`：不第二次 POST，不买回，不把这笔负债当成已还。随后的查询如果读到身份和数量都对得上的执行中列表，可以绑定为已保护，仍然不新下单。超时同样占位。

已存的 `rejected` 且还没有 flatten 记录时，重启也不新开买回。已经开始的 flatten 只允许顺着原记录查询，不新发一笔。

HMAC 或外部 signer、配置文件 SHA-256、UID、时效，以及 borrow / repay / new_order / cancel_order / new_oco / cancel_oco 六个写接口的字段闸都没有放宽。未钉住的批准文件不能进 opener。

## 修前失败，修后通过

修前只跑新用例 `execution.tests.test_margin_cross_oco_unknown`：

```text
Ran 9 tests in 0.291s
FAILED (failures=10)
```

失败点包括：`-2010 Duplicate order sent.` 被 `is_deterministic_pre_exec_oco_reject` 判成真，以及 `-1116` 等码把开仓打成 `protection_failed_flattened`。

修后：

```text
python3 -m unittest discover -s execution/tests -p 'test_*.py'
Ran 403 tests in 4.509s
OK
```

这 403 项是假客户端和本地闸门。不是币安保证金接口回放。

导入检查在子进程里先替换 `socket.socket` / `create_connection`，再导入 `track`、`entry`、`http_client`，退出码 0，输出 `import-ok`。

## 修改文件 SHA-256

| 文件 | SHA-256 |
| --- | --- |
| `execution/margin_cross/errors.py` | `cca0c13b2c09c55eaf994d9f8b7977de48fd8812dc2e0a0e5ce9396a274de75a` |
| `execution/margin_cross/track.py` | `983caf8273ce189651ac0f80912bc12424ead75378f5a5596dffa0ebcb96bc5e` |
| `execution/tests/test_margin_cross_track.py` | `fb5ccfde76d85923f9d6b5d457a61aeb1dbd9684deef02cc32db71e97744fbb9` |
| `execution/tests/test_margin_cross_policy_and_idempotency.py` | `fb3ef76ce2eca8a506eef56a83487f8dd5d57e982d16f86f541bfa0902edf515` |
| `execution/tests/test_margin_cross_oco_unknown.py` | `9cf69b551f961c9e55e06bc3497cc2903fcf809d732048cfb6af23ea5b1c284e` |

本文件的哈希不写进自己体内。提交后的 git blob 以该提交树为准。

## 残留的非代码边界

- 没有实网传输验收，没有私钥，没有借款、下单、还款、划转、部署、合并或撤急停。
- 没有替杨批准最高倍数、借款限额、保证金率或利息预留。现货仓位 3 与 0.72 没有被抄进本轮逻辑。
- 整数错误码仍然不是保证金协议实证。自动“码表 + 空 openOrders 就买回”已关闭；本地在尚未 POST OCO 时，因报价不够支付止损买或买盘数量不够而走的 flatten，还在。那条路径不读交易所拒绝码。
- OTO/OTOCO 仍不提交。`GET /sapi/v1/margin/account` 仍不返回 uid。
- 查询暂时看不到列表时，轨道停在 `protection_unknown`，不假装已平。后续要不要人工买回，由杨决定。
- PR 221、PR 223 和本轮之外的机构模块没有改。
