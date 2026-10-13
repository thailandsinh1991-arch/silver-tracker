# Silver Tracker

Theo dõi giá bạc Phú Quý và giá bạc thế giới, kèm danh mục cá nhân.

- **Trang web:** `index.html`, chạy trên GitHub Pages.
- **Giá:** `data/market.json`, do GitHub Actions (`.github/workflows/update-prices.yml`) cập nhật mỗi 30 phút từ 07:00 đến 22:30 giờ Việt Nam.
- **Nguồn:** giabac.phuquygroup.vn và Kitco. Nếu không đọc được Kitco, giá thế giới lấy theo dòng tham chiếu trên bảng giá Phú Quý.
- **Giao dịch và cảnh báo:** chỉ lưu trong trình duyệt của bạn (localStorage), không nằm trong repo.

Chạy cập nhật thủ công: tab **Actions** → **Cập nhật giá bạc** → **Run workflow**.
