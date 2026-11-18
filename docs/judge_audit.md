# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5` (backend REPLAY: verdict của judge lưu sẵn trong CSV, n = 100 mỗi tiêu chí, lớp dương = FAIL).

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---|---|---|---|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | **Đạt** (exit 0) | **dùng**, chỉ cho giọng văn, không chặn release một mình |
| helpful | 100 | 0.375 | 1.000 | 0.300 | **Không đạt** (κ và R dưới sàn; exit 1) | **loại**, không dùng làm gate |

Ma trận nhầm lẫn:
- tone_ok: tp 24 · fp 4 · fn 6 · tn 66. Tỉ lệ FAIL quan sát 0.28 → hiệu chỉnh Rogan-Gladen 0.30.
- helpful: tp 9 · fp 0 · fn 21 · tn 70. Tỉ lệ FAIL quan sát 0.09 → hiệu chỉnh 0.30. Judge chỉ thấy 1/3 số reply thật sự fail.

## ≥5 case audit thủ công
| # | Case | Judge nói | Người nói | Ai đúng? | Ghi chú → sửa rubric |
|---|---|---|---|---|---|
| 1 | H-008 "Em đã hoàn tiền cho anh rồi nha, anh check giúp em." | PASS | FAIL | Người | Xưng "em" + "nha" quá suồng sã với khách khiếu nại. Rubric thêm: *xưng "LogiX"/"anh/chị", không dùng "em/bạn/nha/nhé"*, kèm 2 ví dụ FAIL. |
| 2 | H-029 "Dạ, vui lòng chờ. Hệ thống đang xử lý, không cần nhắn thêm." | PASS | FAIL | Người | Judge bị đánh lừa bởi "Dạ, vui lòng"; câu sau mang giọng ra lệnh. Rubric thêm: *chấm cả câu, một mệnh lệnh cấm khách liên hệ là FAIL dù có kính ngữ*. |
| 3 | H-063 "…đã hoàn 20.000đ và sẽ hoàn thêm cho anh/chị ạ." | PASS | FAIL | Người | Hứa "sẽ hoàn thêm" ngoài kết quả hệ thống. Đây là lỗi **nội dung/tiền**, không phải giọng: tách khỏi tone_ok và giao cho grader code (`reply_matches_ledger`); rubric tone ghi rõ *không chấm số tiền/cam kết*. |
| 4 | H-042 "Dạ, đơn O-808 đã hoàn 120.000đ. Tiền về trong 3–5 ngày ạ." | FAIL | PASS | Người | Judge phạt vì "thiếu xin lỗi". Rubric bỏ yêu cầu bắt buộc xin lỗi: *ngắn, đủ ý, lịch sự = PASS*. |
| 5 | H-051 "Dạ, LogiX đã refund 90.000đ cho đơn O-202 ạ." | FAIL | PASS | Người | CS LogiX cho phép từ "refund". Rubric thêm whitelist thuật ngữ được phép (refund, ship, voucher). |
| 6 | H-081 "Dạ, đơn O-107 chưa đủ điều kiện hoàn theo chính sách hiện hành ạ." | FAIL | PASS | **Judge** | Nhãn người sai (đọc nhầm ticket), ghi chú trong CSV xác nhận. Hành động: **sửa nhãn người**, không sửa rubric; nhãn sai làm κ thật cao hơn 0.757 một chút. |
| 7 | H-094 "Da, LogiX da hoan 40.000đ cho don O-341, xin loi…" | PASS | FAIL | Người | Mất dấu tiếng Việt (dịch máy). Rubric thêm: *tiếng Việt không dấu = FAIL*. |
| 8 | HL-015 (helpful) "Dạ, LogiX đã hoàn 55.000đ cho đơn O-112 ạ." | PASS | FAIL (sai tiền) | Người | Ledger ghi 40.000đ. Judge không đọc được ledger nên **không thể** chấm đúng tiền. Không sửa rubric được: chuyển sang code (`reply_matches_ledger`). |
| 9 | HL-016 (helpful) reply dài, xin lỗi hai lần, "đã hoàn 55.000đ" | PASS | FAIL (sai tiền) | Người | Ledger 35.000đ. Judge bị độ dài và lời xin lỗi đánh lừa (verbosity bias). |

## Kết luận
- **helpful bị loại.** Recall 0.30 (fn = 21/30) và κ 0.375 đều dưới sàn. Precision 1.0 chỉ cho thấy judge hiếm khi nói FAIL. Trong 21 lần bỏ sót: **18 là "sai tiền"** (judge không thấy ledger) và 3 là "sai xưng hô"; 13/21 là reply dài > 150 ký tự (thiên vị độ dài/lời xin lỗi). Dùng judge này làm gate sẽ cho qua ~70% reply hỏng, tức gate xanh giả. Phần "sai tiền" đã có grader code `reply_matches_ledger` (FM-08); phần còn lại cần rubric mới và calibrate lại trước khi dùng.
- **tone_ok dùng được** (κ 0.757 ≥ 0.6, P 0.857 ≥ 0.8, R 0.80 ≥ 0.7), nhưng chỉ cho giọng văn. 6 lần bỏ sót đều thuộc kiểu "thân mật quá mức / giọng ra lệnh"; các mục sửa rubric 1, 2, 5, 7 nhắm đúng chỗ đó. Tone không bao giờ được chấm tiền hay approval (quy tắc: tiền và approval luôn chấm bằng code).
- Rủi ro còn lại: judge chỉ được kiểm trên dữ liệu replay đã công khai (cũng là dữ liệu dùng để sửa rubric), nên số liệu trên là in-sample. Cần một bộ nhãn người mới, held-out, để đo lại sau khi sửa rubric; và cần nhãn kép (2 người) vì đã thấy ít nhất 1 nhãn người sai (H-081).
