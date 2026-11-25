# Ninja School MP — 17/11/2026

Đi kèm client Ninja School Offline **v1.26.82**. Không đổi giao thức v2, không
đổi opcode hay khoá wire: client .78–.81 vẫn kết nối được.

## Sửa lỗi

- **Tên gia tộc trùng**: người chơi được gán tên phụ (`Leaf_1`) nay luôn nhận
  `clan_name_assigned`. Trước đây thông báo chỉ gửi ở nhánh không tạo được gia
  tộc nên client vẫn hiển thị tên cũ của mình.
- **Khách rớt mạng khi đang cừu sát**: đối thủ nhận `cuu_sat_end` và trạng thái
  cừu sát được xoá. Trước đây đối thủ giữ mục tiêu đã rời phòng cho tới khi tự
  đổi map.
- **Khách bấm rời phòng** (`leave`) khi đang tổ đội, giao dịch hoặc tỷ thí:
  dọn giống như rớt mạng (`party_closed`/roster mới, `trade_cancel`,
  `duel_end`). Trước đây chỉ rớt mạng mới dọn, rời chủ động để lại tổ đội và
  giao dịch treo.
- Lời mời tổ đội, gia tộc, kết bạn, giao dịch, tỷ thí liên quan tới người đã
  rời được xoá ngay.
- **Phòng trống trong lúc chờ chủ phòng kết nối lại**: phòng được giữ tới hết
  45 giây chờ để chủ phòng còn `resume` được; hết hạn mới đóng. Khi đóng, dữ
  liệu lưu của phòng được xoá (trước đây còn sót trong bộ nhớ và file state).

## Ổn định và bảo vệ

- Lỗi bất ngờ khi xử lý một lệnh (`AttributeError`, `IndexError`, tràn số…)
  trả `bad_request` và ghi log `warning` thay vì làm rớt kết nối người gửi.
  Rớt kết nối chủ phòng làm cả phòng phải chờ, nên một gói tin lỗi không còn
  gây việc đó.
- Bộ giải mã từ chối dữ liệu lồng quá 16 tầng. Một frame 256 KiB toàn danh
  sách lồng nhau trước đây vượt giới hạn đệ quy của Python.
- Bảng đếm sai mật khẩu giới hạn 4.096 dòng; khoá đang hiệu lực được giữ.

## Kiểm thử

`python -m pytest -q`: 172 test. Thêm `tests/test_member_departure.py` (7 ca)
cho các mục trên; các ca này lỗi trên mã trước khi sửa.
