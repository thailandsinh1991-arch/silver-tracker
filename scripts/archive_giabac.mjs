// Lưu trữ biểu đồ giá bạc Phú Quý từ giabac.vn trước khi trang gốc xoá dữ liệu cũ (trang chỉ giữ 1 năm).
// Chạy mỗi ngày bởi .github/workflows/archive-giabac.yml. Mỗi lần chạy lưu vào archive/:
//   charts/YYYY-MM-DD.jpg          ảnh phần biểu đồ (khung 1Y)
//   pages/YYYY-MM-DD.jpg           ảnh toàn trang (dự phòng)
//   giabac/YYYY-MM-DD/NN.json      mọi gói dữ liệu JSON trang tải về (thường chứa chuỗi giá của biểu đồ)
//   charts.json                    danh sách ảnh để app hiển thị
// Không bao giờ xoá tệp cũ.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const ARCHIVE = path.join(ROOT, "archive");
const URL_ = process.env.GIABAC_URL || "https://giabac.vn/";
const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Ho_Chi_Minh" }).format(new Date());
const SKIP_HOSTS = /google|gstatic|facebook|fbcdn|doubleclick|tiktok|zalo|clarity|hotjar|analytics|ads|cloudflareinsights/i;

fs.mkdirSync(path.join(ARCHIVE, "charts"), { recursive: true });
fs.mkdirSync(path.join(ARCHIVE, "pages"), { recursive: true });
const rawDir = path.join(ARCHIVE, "giabac", today);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1600 }, locale: "vi-VN", timezoneId: "Asia/Ho_Chi_Minh", deviceScaleFactor: 1 });
const captured = [];
page.on("response", async (res) => {
  try {
    const url = res.url();
    if (SKIP_HOSTS.test(new URL(url).hostname)) return;
    const type = (res.headers()["content-type"] || "").toLowerCase();
    if (!/json|javascript|text\/plain/.test(type) && !/\.json(\?|$)|\/api\//i.test(url)) return;
    const body = await res.text();
    if (body.length < 50 || body.length > 8_000_000) return;
    const t = body.trim();
    if (!(t.startsWith("{") || t.startsWith("["))) return;
    JSON.parse(t);
    captured.push({ url, status: res.status(), body: t });
  } catch { /* bỏ qua phản hồi không phải JSON */ }
});

let ok = false, note = "";
try {
  await page.goto(URL_, { waitUntil: "networkidle", timeout: 90_000 });
} catch (e) { note = `goto: ${e.message.split("\n")[0]}`; }
try {
  const btn = page.getByText("1Y", { exact: true }).first();
  if (await btn.isVisible({ timeout: 5000 })) { await btn.click(); await page.waitForLoadState("networkidle", { timeout: 20_000 }).catch(() => {}); }
} catch { /* trang có thể đã mặc định 1Y */ }
await page.waitForTimeout(3500);

// Ảnh phần biểu đồ: từ tiêu đề "Giá bạc Phú Quý 999" tới hết khung biểu đồ (canvas lớn nhất).
try {
  const canvases = await page.locator("canvas").all();
  let best = null;
  for (const c of canvases) {
    const b = await c.boundingBox();
    if (b && b.width > 300 && b.height > 150 && (!best || b.width * b.height > best.width * best.height)) best = b;
  }
  if (best) {
    let top = Math.max(0, best.y - 420);
    try {
      const h = page.getByText(/Giá bạc Phú Quý 999/i).first();
      const hb = await h.boundingBox({ timeout: 3000 });
      if (hb && hb.y < best.y && best.y - hb.y < 900) top = Math.max(0, hb.y - 20);
    } catch { /* giữ khoảng mặc định */ }
    const left = Math.max(0, best.x - 40);
    const clip = { x: left, y: top, width: Math.min(1280 - left, best.width + 80), height: best.y + best.height + 60 - top };
    await page.screenshot({ path: path.join(ARCHIVE, "charts", `${today}.jpg`), type: "jpeg", quality: 82, clip, fullPage: true });
    ok = true;
  } else note ||= "không thấy khung biểu đồ";
} catch (e) { note ||= `chart: ${e.message.split("\n")[0]}`; }
await page.screenshot({ path: path.join(ARCHIVE, "pages", `${today}.jpg`), type: "jpeg", quality: 70, fullPage: true }).catch(() => {});
await browser.close();

if (captured.length) {
  fs.mkdirSync(rawDir, { recursive: true });
  const index = captured.map((c, i) => {
    const f = `${String(i + 1).padStart(2, "0")}.json`;
    fs.writeFileSync(path.join(rawDir, f), c.body);
    return { file: f, url: c.url, status: c.status, bytes: c.body.length };
  });
  fs.writeFileSync(path.join(rawDir, "_nguon.json"), JSON.stringify({ trang: URL_, ngay: today, tep: index }, null, 1));
}

// Danh sách ảnh cho app (quét lại thư mục, không mất ảnh nào).
const list = fs.readdirSync(path.join(ARCHIVE, "charts")).filter((f) => /\.(jpe?g|png|webp)$/i.test(f)).sort().reverse()
  .map((f) => ({ file: `archive/charts/${f}`, date: f.slice(0, 10), source: "giabac.vn" }));
const prev = fs.existsSync(path.join(ARCHIVE, "charts.json")) ? JSON.parse(fs.readFileSync(path.join(ARCHIVE, "charts.json"), "utf8")) : { charts: [] };
const old = Object.fromEntries((prev.charts || []).map((c) => [c.file, c]));
const nowIso = new Date().toISOString();
fs.writeFileSync(path.join(ARCHIVE, "charts.json"), JSON.stringify({
  updatedAt: nowIso,
  charts: list.map((c) => ({
    ...c,
    at: old[c.file]?.at || (c.file.endsWith(`/${today}.jpg`) ? nowIso : `${c.date}T12:00:00+07:00`),
    note: old[c.file]?.note || "Biểu đồ giá bạc Phú Quý 999 · khung 1 năm · ₫/lượng",
  })).sort((x, y) => Date.parse(y.at) - Date.parse(x.at)),
}, null, 1) + "\n");

console.log(`Ảnh biểu đồ: ${ok ? "đã lưu" : "KHÔNG lưu được"} · gói dữ liệu JSON: ${captured.length}${note ? " · " + note : ""}`);
process.exit(ok || captured.length ? 0 : 1);
