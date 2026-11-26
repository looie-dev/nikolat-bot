---
title: XAUUSD Trading Bot
emoji: 📈
colorFrom: yellow
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# XAUUSD Auto-Trading Bot (macOS / Linux / Hugging Face Spaces + Telegram)

Bot giao dịch vàng (**XAUUSD**) viết bằng Python thuần (không cần cài thư viện ngoài hay Windows VM), hỗ trợ chạy trực tiếp trên macOS với **OANDA Practice**, **Capital.com Demo**, hoặc **Paper Mode** giả lập cục bộ.

## 1. Chiến lược giao dịch (Multi-Confluence Trend Pullback)

Kết hợp các bộ lọc hiệu quả đã kiểm chứng trên XAUUSD:
- **Lọc phiên thanh khoản (`07:00–17:00 UTC`):** Tập trung vào phiên London và giao thoa London–New York.
- **Xác định xu hướng (`EMA 50 / EMA 200`):** Chỉ BUY khi `EMA50 > EMA200` và `EMA50` đang dốc lên; chỉ SELL khi `EMA50 < EMA200` và `EMA50` đang dốc xuống.
- **Điểm vào Pullback:** Giá hồi về vùng `EMA50` trong vòng 5 nến gần nhất rồi đóng nến xác nhận quay lại xu hướng.
- **Động lượng (`RSI 14` + `MACD 12,26,9`):** RSI nằm trong vùng lành mạnh (`40–68` cho BUY, `32–60` cho SELL) và đang quay đầu đúng hướng + MACD Histogram cắt/móc theo xu hướng.
- **SL / TP động thuần theo `ATR(14)`:** Mặc định `STRATEGY_MODES=m5_scalp,m15_intraday` dùng `m5_scalp` (`EMA 50/200`, `ADX >= 15`, lọc xu hướng khung lớn `M15 EMA 20/50`, `SL = 1.2 × ATR`, `RR 1:1.7`) và `m15_intraday` (`EMA 20/50`, `ADX >= 18`, `SL = 1.0 × ATR`, `RR 1:2.0`), lọc phiên `07:00–17:00 UTC`.
- **Theo dõi lệnh thủ công & Quản lý lệnh thông minh:** Bấm nút `✅` / `🛡` / `🔒` hoặc dùng các lệnh Telegram (`/status`, `/news`, `/pause`, `/resume`, `/sl <giá>`, `vào <giá>`, `đóng <giá>`).
- **Bộ lọc an toàn & Quản lý vốn:**
  - Chặn mở lệnh mới `±15 phút` quanh tin **USD High-Impact** và nhắc trước `30 phút` khi đang giữ lệnh mở; nhắc cuối tuần vào tối thứ Sáu.
  - Chặn khi **spread** vượt `MAX_SPREAD_USD`, tự giảm phân nửa risk sau chuỗi thua liên tiếp (`LOSS_STREAK_REDUCE_AFTER=2`).
  - Lưu trạng thái bền vững ra JSON (`state_store.py`) và hỗ trợ đồng bộ qua Upstash Redis REST nếu chạy Render Free Tier.

## 2. Cấu trúc thư mục

- `bot.py` — Vòng lặp chính theo dõi nến đóng, kiểm tra bộ lọc, vào lệnh và theo dõi đóng lệnh.
- `strategy.py` — Logic chiến lược Multi-Confluence Trend Pullback.
- `indicators.py` — Các chỉ báo EMA, RSI, MACD, ATR, ADX (Python thuần).
- `risk_manager.py` — Tính size theo rủi ro %, kiểm tra giới hạn ngày và spread.
- `news_filter.py` — Lấy lịch tin USD High-Impact từ FairEconomy/ForexFactory.
- `notifier.py` — Gửi thông báo qua Telegram Bot API (vào lệnh, bỏ qua lệnh, chốt lời/cắt lỗ, heartbeat).
- `brokers/` — Adapter kết nối `oanda.py`, `capital.py`, `paper.py`.
- `backtest.py` — Công cụ backtest trên dữ liệu CSV lịch sử (có tính spread).

## 3. Cài đặt & Cấu hình

1. Sao chép file mẫu cấu hình:
   ```bash
   cd /Users/looie/xauusd-bot
   cp .env.example .env
   ```

2. **Tạo Telegram Bot:**
   - Nhắn `/newbot` cho [@BotFather](https://t.me/BotFather) để lấy `TELEGRAM_BOT_TOKEN`.
   - Nhắn 1 tin nhắn bất kỳ cho bot vừa tạo, sau đó mở link `https://api.telegram.org/bot<TOKEN>/getUpdates` để lấy `chat.id` điền vào `TELEGRAM_CHAT_ID`.

3. **Chọn tài khoản thử nghiệm (Demo) trong `.env`:**
   - **OANDA Practice:** Tạo tài khoản Practice trên OANDA → Manage API Access → lấy token điền vào `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID`, đặt `BROKER_PROVIDER=oanda`.
   - **Capital.com Demo:** Bật 2FA → Settings → API integrations → tạo key, điền `CAPITAL_API_KEY`, `CAPITAL_IDENTIFIER`, `CAPITAL_PASSWORD`, đặt `BROKER_PROVIDER=capital`.
   - **Paper Mode (chạy thử ngay không cần tạo tài khoản sàn):** Giữ `BROKER_PROVIDER=paper`.

## 4. Chạy Bot & Backtest

- Chạy kiểm tra 1 chu kỳ (hoặc test thông báo với `--dry-run`):
  ```bash
  python3 bot.py --once --dry-run
  ```
- Chạy tự động liên tục trên tài khoản Demo:
  ```bash
  python3 bot.py
  ```
- Chạy Unit Tests:
  ```bash
  python3 -m unittest discover -s tests -v
  ```
- Chạy Backtest trên file CSV OHLCV:
  ```bash
  python3 backtest.py --csv /path/to/xauusd_h1.csv --spread 0.30
  ```
