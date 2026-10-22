# Silver Tracker

Theo dõi giá bạc Phú Quý và giá bạc thế giới, kèm danh mục cá nhân.

- **Trang web:** `index.html`, chạy trên GitHub Pages.
- **Giá:** `data/market.json`, do GitHub Actions (`.github/workflows/update-prices.yml`) cập nhật mỗi 15 phút từ 07:00 đến 22:45 giờ Việt Nam.
- **Nguồn:** giabac.phuquygroup.vn và Kitco. Nếu không đọc được Kitco, giá thế giới lấy theo dòng tham chiếu trên bảng giá Phú Quý.
- **Giao dịch và cảnh báo:** chỉ lưu trong trình duyệt của bạn (localStorage), không nằm trong repo.

Chạy cập nhật thủ công: tab **Actions** → **Cập nhật giá bạc** → **Run workflow**.

## Lưu trữ vĩnh viễn (thư mục `archive/`)

Trang giabac.vn chỉ giữ biểu đồ 1 năm gần nhất, nên mọi thứ dưới đây được giữ lại trong repo và **không bao giờ bị xoá**:

- `archive/charts/` – ảnh biểu đồ giá bạc Phú Quý 999 (khung 1 năm). GitHub Actions (`.github/workflows/archive-giabac.yml`) tự chụp lúc 17:05 mỗi ngày. `archive/charts.json` là danh sách ảnh để app hiển thị.
- `archive/pages/` – ảnh toàn trang giabac.vn mỗi ngày (dự phòng).
- `archive/giabac/YYYY-MM-DD/` – dữ liệu giá thô (JSON) mà giabac.vn tải về khi vẽ biểu đồ; `_nguon.json` ghi nguồn của từng tệp.
- `archive/giabac-lich-su-kg.csv`, `archive/giabac-lich-su-luong.csv` – chuỗi giá của biểu đồ giabac.vn (giá gốc theo từng đơn vị, nhiều điểm mỗi ngày). Bắt đầu từ 16/10/2025; mỗi ngày nối thêm ngày hôm trước.
- `archive/gia-phu-quy.csv` – sổ giá Phú Quý: mỗi lần Phú Quý đăng bảng giá mới, toàn bộ bảng được ghi thêm (mở được bằng Excel/Google Sheets).
- `archive/gia-the-gioi.csv` – sổ giá bạc thế giới (XAG/USD) theo từng lần cập nhật.

Ngoài ra, mọi phiên bản cũ của các tệp đều còn trong lịch sử commit của GitHub.

Chụp thủ công: tab **Actions** → **Lưu trữ biểu đồ giabac.vn** → **Run workflow**.

## Cảnh báo qua Telegram và báo động ntfy

Sau mỗi lần cập nhật giá, `scripts/check_alerts.py` so giá với cảnh báo của từng người (lưu trên Firebase theo tài khoản Google) và:
- gửi tin nhắn qua bot Telegram của repo cho mọi cảnh báo vừa kích hoạt;
- cho điện thoại reo qua ứng dụng [ntfy](https://ntfy.sh) (miễn phí, mã nguồn mở) với cảnh báo được đánh dấu “báo động”. Mỗi người có một mã kênh ngẫu nhiên do app tạo; mã kênh như mật khẩu, không chia sẻ.

Máy chủ kiểm tra giá khoảng mỗi 2 phút từ 07:00 đến 22:50. Mỗi cảnh báo chỉ báo một lần khi giá vượt mức; giá quay lại thì sẵn sàng báo lần sau.

Cần 2 GitHub Secrets (Settings → Secrets and variables → Actions):
- `TELEGRAM_BOT_TOKEN` – mã bot lấy từ @BotFather.
- `FIREBASE_SERVICE_ACCOUNT` – toàn bộ nội dung tệp JSON khoá dịch vụ (Firebase → Project settings → Service accounts → Generate new private key).

Gửi thử: tab **Actions** → **Cập nhật giá bạc** → **Run workflow** → đánh dấu “Gửi tin nhắn Telegram … thử nghiệm”.

