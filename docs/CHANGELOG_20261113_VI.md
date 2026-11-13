# Sửa tỷ thí và hiển thị gia tộc — 13/11/2026

## Server

- Khi người đang tỷ thí đổi map hoặc đổi khu, server xử người rời khu vực
  thua, gửi kết quả cho cả hai và xóa quan hệ tỷ thí ngay. Áp dụng cho cả gói
  `map` lẫn `state`; di chuyển trong cùng map/khu vẫn giữ trận đấu.
- Xóa cả hai liên kết trước khi gửi kết quả để hai người cùng đổi map/khu
  không tạo hai kết quả. Chặn bắt đầu trận khác khi một bên còn đang tỷ thí.
- Gia tộc được cập nhật trong appearance khi nhận lời vào tộc, rời tộc hoặc
  bị mời ra. Thông báo rời/bị mời ra được gửi cho những người trong phòng;
  snapshot sau đó giữ đúng tên hiện tại.

## Client đi kèm

Cần cập nhật **Ninja School Offline .77** cho các sửa phía game:

- Gói đánh hỗn hợp quái/người (`4`/`73`) chuyển phần mục tiêu người sang
  Multiplayer. Phần đánh quái vẫn hoạt động; cả đòn chỉ trừ MP và bắt đầu
  hồi chiêu một lần.
- Client kết thúc tỷ thí được dù đối thủ đã rời màn hình.
- Gói tạo nhân vật mang tên gia tộc và byte chức vụ theo đúng wire native.
  Đổi tên gia tộc cũng làm mới nhãn nhân vật đang hiển thị.

## Kiểm tra

- Toàn bộ **164 unittest PASS**.
- `tests/test_reported_duel.py`: TCP thật với hai client, sát thương/ID hiệu
  ứng hai chiều, mọi tổ hợp map/khu và gói `map`/`state`, giữ trận khi chỉ
  di chuyển, hai người cùng rời map, không ghi đè trận đang mở, snapshot
  gia tộc, nhận lời/rời/bị mời khỏi tộc.
- Test liên repo phát gói từ bytecode gửi native, qua engine Java, server
  TCP và trả về engine cả hai phía; kiểm tra đúng HP và gói hiệu ứng.
- Test bytecode native tái hiện handler kết thúc trận cũ lỗi khi đối thủ
  không còn trong map và xác nhận bản vá dọn trạng thái được.

Sau khi cập nhật source server, khởi động lại process server để áp dụng.
Chưa có xác nhận chạy trên hai điện thoại thật.
