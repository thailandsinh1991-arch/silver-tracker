# Silver Tracker

Theo dõi giá bạc Phú Quý và giá bạc thế giới, kèm danh mục cá nhân.

- **Trang web:** `index.html`, chạy trên GitHub Pages.
- **Giá:** `data/market.json`, do GitHub Actions (`.github/workflows/update-prices.yml`) cập nhật mỗi 30 phút từ 07:00 đến 22:30 giờ Việt Nam.
- **Nguồn:** giabac.phuquygroup.vn và Kitco. Nếu không đọc được Kitco, giá thế giới lấy theo dòng tham chiếu trên bảng giá Phú Quý.
- **Giao dịch và cảnh báo:** chỉ lưu trong trình duyệt của bạn (localStorage), không nằm trong repo.

Chạy cập nhật thủ công: tab **Actions** → **Cập nhật giá bạc** → **Run workflow**.

## Lưu trữ vĩnh viễn (thư mục `archive/`)

Trang giabac.vn chỉ giữ biểu đồ 1 năm gần nhất, nên mọi thứ dưới đây được giữ lại trong repo và **không bao giờ bị xoá**:

- `archive/charts/` – ảnh biểu đồ giá bạc Phú Quý 999 (khung 1 năm). GitHub Actions (`.github/workflows/archive-giabac.yml`) tự chụp lúc 17:05 mỗi ngày. `archive/charts.json` là danh sách ảnh để app hiển thị.
- `archive/pages/` – ảnh toàn trang giabac.vn mỗi ngày (dự phòng).
- `archive/giabac/YYYY-MM-DD/` – dữ liệu giá thô (JSON) mà giabac.vn tải về khi vẽ biểu đồ; `_nguon.json` ghi nguồn của từng tệp.
- `archive/gia-phu-quy.csv` – sổ giá Phú Quý: mỗi lần Phú Quý đăng bảng giá mới, toàn bộ bảng được ghi thêm (mở được bằng Excel/Google Sheets).
- `archive/gia-the-gioi.csv` – sổ giá bạc thế giới (XAG/USD) theo từng lần cập nhật.

Ngoài ra, mọi phiên bản cũ của các tệp đều còn trong lịch sử commit của GitHub.

Chụp thủ công: tab **Actions** → **Lưu trữ biểu đồ giabac.vn** → **Run workflow**.

