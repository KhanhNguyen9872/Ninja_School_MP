# Ninja School Multiplayer Server

Máy chủ phòng Multiplayer độc lập cho **Ninja School Offline**. Server quản lý
phòng chơi và đồng bộ trạng thái tạm thời; dữ liệu nhân vật vẫn được lưu riêng
trên thiết bị bằng RMS.

## Tính năng

- Tối đa 12 người trong một phòng.
- Đồng bộ nhân vật, bản đồ, chiến đấu, quái và vật phẩm rơi.
- Hỗ trợ chat, tổ đội, giao dịch, tỷ thí, gia tộc và hoạt động chung.
- Lưu snapshot phòng để khôi phục sau khi server khởi động lại.
- Hỗ trợ TCP và UDP trên cùng cổng; phía game mặc định dùng TCP.
- Giao thức nhị phân v2, UTF-8, chạy bằng `asyncio` và không cần thư viện ngoài.

## Yêu cầu

- Python 3.10 trở lên.
- Mở cổng TCP và UDP `8765` nếu chạy trên VPS hoặc máy chủ công cộng.

## Chạy server

```bash
python server.py --host 0.0.0.0 --port 8765
```

Mặc định server mở cả TCP và UDP. Có thể giới hạn bằng
`--transport tcp`, `--transport udp` hoặc `--transport both`.

Windows có thể chạy trực tiếp `run-server.cmd`.

Lưu trạng thái phòng vào file riêng:

```bash
python server.py --host 0.0.0.0 --port 8765 --state-file rooms.json
```

Hoặc chạy dưới dạng package:

```bash
python -m nso_server
```

## Docker

```bash
docker compose up -d --build
```

## Kiểm thử

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

## Cấu trúc chính

```text
server.py          Launcher tương thích
nso_server/        Mã nguồn server
tests/             Kiểm thử tích hợp
pyproject.toml     Cấu hình package Python
Dockerfile         Image triển khai
```

> Server chỉ giữ trạng thái phòng và thế giới dùng chung. Đây không phải máy
> chủ tài khoản và không đọc hoặc thay thế dữ liệu nhân vật RMS.
