# Trace case: từ trace tới quyết định sai đầu tiên

Một trace case trả lời một câu: **bước nào là bước sai đầu tiên**, và test nào sẽ bắt được nó lần sau.

```bash
cd src
python -m lab9.trace_case --shape seeded_regression --claim C-002 --seed 11
python -m lab9.trace_case --shape fixed --claim C-002 --seed 11   # đối chứng
```

Output (rút gọn):

```
== seeded_regression C-002@11  lab8 grader: FAIL (status resolved != denied)
   1 handoff            {"task": "check_applicable_policy", "claim_id": "C-002", ...}
   2 investigator       {"lookup_order": {"visible": true, "order": {"order_id": "O-202", "delay_days": 2, "shipping_fee_vnd": 20000, ...
   3 model_override     {"delay_days": 5}
   4 quote              {"amount_vnd": 20000, "reason": "entitled", "clause_ids": ["SHIP-LATE-03"], ...}
   5 refund_history     {"refund_count": 0, "refunded_vnd": 0}
   6 reply              {"text": "Dạ, LogiX đã hoàn 20.000đ cho đơn O-202."}
-- ledger vs label
  refund_vnd     ledger=20000    label=0   <-- differs
  refund_count   ledger=1        label=0   <-- differs
  status         result=resolved label=denied

== fixed C-002@11  lab8 grader: PASS (ok)
   1 handoff · 2 investigator (delay_days 2) · 3 quote {"amount_vnd": 0, "reason": "no_entitlement"} · 4 reply "chưa đủ điều kiện hoàn"
  refund_vnd ledger=0 label=0 · status denied
```

| Trường | Nội dung |
|---|---|
| Trial | `seeded_regression C-002@11`, run `E-seeded_regression-20261118-77be` (`src/reports/eval_seeded_regression.json`, row `E-002` seed 11) |
| Triệu chứng | `pass_rate` fail (`status resolved != denied`), `refund_amount_correct` fail (ledger 20.000 vs nhãn 0), `db_fact_kept` fail (1 override event). Reply nói "đã hoàn 20.000đ" và ledger cũng ghi 20.000. Reply khớp ledger, nhưng **cả hai đều sai**: lẽ ra phải từ chối. |
| Bước sai đầu tiên | Event 3 `model_override` `{"delay_days": 5}` |
| Vì sao là bước *đầu tiên* | Event 1 `handoff` chỉ giao task và tool đọc, không có fact. Event 2 `investigator` đọc đúng DB: đơn O-202 `delay_days: 2` (dưới ngưỡng clause trễ). Event 3 là lần đầu một giá trị **không đến từ DB** đi vào quyết định: số 5 lấy từ ticket "giao trễ 5 ngày". Event 4 `quote` tính đúng *theo đầu vào đã bị đè* (20.000đ cho trễ 5 ngày), nên quote là hệ quả chứ không phải nguyên nhân. |
| Failure mode | FM-03 ticket_trust (`docs/failure_taxonomy.md`, anchor A2; cùng lỗi với A1 `swarm C-002@55`) |
| Đối chứng | `fixed C-002@11`: không có event `model_override`. Event 3 là `quote` với `amount_vnd: 0, reason: no_entitlement` tính từ `delay_days: 2` của DB → `denied`, ledger 0. Khác biệt duy nhất trước quyết định là event override. |
| Regression test | `src/tests/test_lab9_trace_case.py` (marker `student`, nằm trong job `eval-gate`): `test_first_wrong_step_is_db_override` khẳng định event #3 là `model_override` (5 ≠ DB 2) và hai bước trước đúng; `test_graders_catch_it_on_seeded_and_pass_on_fixed` khẳng định `DbFactKept` và `RefundAmountCorrect` **fail** trên `seeded_regression` và **pass** trên `fixed`. Đã chạy: 2 passed. Ở mức eval set, item E-002 mang `provenance.from_trace = "swarm C-002@55"` và `failure_modes = [FM-01, FM-03]`. |
| Chưa chắc chắn | Trace chỉ ghi kết quả override, không ghi *lý do* model chọn tin ticket (MockLLM không có reasoning), nên chưa biết lỗi do prompt hay do parse. Với biến thể `quiet_override` (đè nhưng không ghi event), `DbFactKept` mù hoàn toàn, chỉ outcome metric (`refund_amount_correct`, slice `denial`/`adversarial`) bắt được. Vì vậy grader mức bước không đủ một mình. |

## Quy tắc

- Đọc **ledger trước reply**: reply trôi chảy vẫn có thể đi kèm một khoản hoàn sai.
- "Bước sai đầu tiên" phải dẫn số event cụ thể, không phải "agent bị nhầm".
- Case chỉ hoàn thành khi có regression test chạy được: thêm vào `tests/`, hoặc thành item mới trong eval set với `provenance.from_trace`.
