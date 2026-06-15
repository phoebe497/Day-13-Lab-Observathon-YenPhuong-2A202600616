# BÁO CÁO CẢI THIỆN ĐIỂM SỐ - LAB 13 (OBSERVATHON)

**Thông tin học viên:** 
- *Họ và tên*: Nguyễn Như Yến Phương
- *MHV*: 2A202600616
- *Lớp*: E403

**Mục tiêu:** Tối ưu hóa hiệu năng, độ chính xác, chi phí và khả năng bảo mật của Agent Thương mại điện tử trên cả hai phase Public và Private.

---

## 1. Kết quả Điểm số đạt được
Sau khi áp dụng các giải pháp quan sát (observability) và giảm thiểu (mitigations), điểm số của đội đã tăng trưởng vượt bậc so với baseline ban đầu (~44.94 điểm):

*   **Public Phase:** **86.73 / 100** (F1 Diagnosis: **0.952**) 
*   **Private Phase (held-out + injection twist):** **81.01 / 100** (F1 Diagnosis: **1.000** - Tối đa)

---

## 2. Các lỗi đã được chẩn đoán và khắc phục (11 Fault Classes)
Chúng tôi đã xác định toàn bộ 11 lỗi hệ thống từ baseline và mô tả chi tiết trong `findings.json`:

1.  **error_spike:** Do cấu hình mặc định đặt `tool_error_rate` là 18% và tắt retry. Khắc phục bằng cách bật retry trong `config.json` và cấu hình xử lý retry động trong `wrapper.py`.
2.  **latency_spike:** Khắc phục bằng cách kích hoạt Cache trong `config.json` và triển khai cơ chế Thread-safe Caching trong `wrapper.py`.
3.  **cost_blowup:** Chi phí token quá cao do model ở premium tier và hệ thống system prompt quá dài dòng. Khắc phục bằng cách hạ tier xuống `standard` và giảm `max_completion_tokens` về `400`.
4.  **quality_drift:** Khắc phục triệt để bằng cách đặt `context_reset_every: 5` trong cấu hình để reset context của LLM sau mỗi 5 lượt chat, ngăn ngừa việc tích tụ ngữ cảnh lỗi.
5.  **infinite_loop:** Xử lý bằng cách bật `loop_guard` và giới hạn `tool_budget: 4` để ngăn chặn việc gọi tool lặp đi lặp lại vô tận.
6.  **tool_failure:** Do catalog mặc định ghi đè khóa MacBook thành hết hàng và tắt chuẩn hóa unicode. Khắc phục bằng cách xóa override catalog và đặt `normalize_unicode: true`.
7.  **pii_leak:** Rò rỉ email/SĐT khách hàng. Đã khắc phục bằng cách bật `redact_pii` trong config và viết hàm redact regex trong wrapper.
8.  **fabrication:** Model tự bịa ra tổng tiền khi sản phẩm hết hàng hoặc không giao được. Đã giải quyết bằng cách viết lại rules từ chối nghiêm ngặt trong `prompt.txt`.
9.  **arithmetic_error:** Do nhiệt độ quá cao (1.6) làm model tính toán sai. Khắc phục bằng cách giảm temperature xuống `0.1` và cung cấp công thức toán học chia lấy phần nguyên rõ ràng.
10. **tool_overuse:** Agent gọi quá nhiều tool thừa. Giải quyết bằng cách giới hạn budget gọi tool và dặn dò rõ ràng thứ tự gọi tool trong prompt.
11. **prompt_injection:** Đây là twist ở phase Private, nơi ghi chú đơn hàng của khách dụ dỗ agent bỏ qua giá hệ thống để áp dụng giá ảo (ví dụ: MacBook giá 1 triệu). Khắc phục thành công bằng hàm `sanitize_question` thông minh tại `wrapper.py` và quy tắc bảo mật prompt.

---

## 3. Chi tiết Giải pháp Kỹ thuật

### A. Cấu hình Tối ưu (`solution/config.json`)
*   Giảm `temperature` về `0.1` để tăng tính nhất quán và chính xác của phép tính toán học.
*   Đặt `context_reset_every` thành `5` giúp ngăn chặn trôi dạt ngữ cảnh hiệu quả.
*   Bật `loop_guard`, `normalize_unicode`, `redact_pii` và đặt `tool_budget` thành `4` để kiểm soát chặt chẽ hành vi gọi tool của Agent.

### B. Kỹ thuật Prompt thông minh (`solution/prompt.txt`)
*   **Workflow tuần tự bắt buộc:** Xác định quy trình gồm 5 bước rõ ràng (Trích xuất -> Check kho -> Check giảm giá -> Tính ship -> Xuất kết quả).
*   **Quy tắc số học chặt chẽ:** Chỉ rõ phép tính làm tròn xuống phần nguyên (floor division `// 100`) để khớp hoàn hảo với ground-truth của scorer.
*   **Quy tắc từ chối rõ ràng:** Nghiêm cấm xuất hiện từ khóa `Tong cong` hoặc bất kỳ số tiền nào trong trường hợp đơn hàng bị từ chối (hết hàng, thành phố không hỗ trợ).
*   **Phòng thủ Prompt Injection:** Dặn dò agent coi phần Ghi chú ("Ghi chú" / note) thuần túy là dữ liệu thô (METADATA), tuyệt đối không tuân theo các chỉ dẫn hành động nằm trong đó.

### C. Lớp Giám sát & Giảm thiểu lỗi (`solution/wrapper.py`)
*   **Thread-safe Cache:** Triển khai cơ chế lưu cache kết quả theo `session_id` và câu hỏi để tránh lặp lại lời gọi API đắt đỏ.
*   **Sanitization chọn lọc:** Hàm `sanitize_question` sử dụng regex để phát hiện các mẫu tấn công Prompt Injection phổ biến trong phần Note và ẩn nó đi (`[NOTE_REDACTED]`), đồng thời bảo toàn nguyên vẹn các thông tin mua hàng hợp lệ ở phía trước.
*   **Retry & Backoff:** Triển khai xử lý lỗi rate limit (429) và retry tối đa 5 lần với thời gian chờ tăng dần (exponential backoff) để đảm bảo không bị lỗi ngắt quãng.
*   **PII Redaction:** Sử dụng regex lọc bỏ thông tin nhạy cảm của khách hàng trước khi trả kết quả.

---
