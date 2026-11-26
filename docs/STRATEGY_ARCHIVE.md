# XAUUSD Strategy Reference & Backtest Archive

Tài liệu lưu trữ cấu hình chiến lược đang chạy thực tế trên bot và toàn bộ các mode/biến thể đã backtest đối chứng trên dữ liệu **Capital.com `GOLD`** (`$100` vốn đầu, spread `$0.50`, lot `0.01–0.10`, Exness `contract_size=100`, bộ lọc tin USD tuần này & Smart SL Manager) để dễ tra cứu lại về sau.

---

## 1. Cấu hình đang chạy (`STRATEGY_MODES=ict_intraday,m15_intraday`)

| Mode | Khung vào | Logic cốt lõi | Lọc khung lớn | Giờ giao dịch (UTC / VN) | SL / TP |
| :--- | :---: | :--- | :---: | :---: | :--- |
| **`ict_intraday`** | `M5` | Quét thanh khoản **Asia H/L (`00–07 UTC`) / Swing H/L** → nến xung lực `>= 0.4×ATR` tạo **MSS + Fair Value Gap (FVG)** | `H1 EMA 20/50` | Killzones `07–10` & `12–16 UTC` (`14–17` & `19–23 VN`) | SL sau điểm quét (`0.8–2.5×ATR`), `RR 1:2.0` |
| **`m15_intraday`** | `M15` | `EMA 20/50` + `ADX >= 18` + hồi về `EMA 20` (5 nến) + `RSI` + `MACD` | `H1 EMA 20/50` | `07–17 UTC` (`14:00–00:00 VN`) | `SL = 1.0×ATR`, `RR 1:2.0` |

- **Quản lý lệnh thông minh (`evaluate_trade_management`):**
  - Cả 2 mode gợi ý dời SL hòa vốn (`+50% TP`, đệm `+$0.50`) và khóa lời (`+75% TP` → khóa `+45% TP`) khi nến hụt lực.
  - Riêng `ict_intraday` **bỏ cảnh báo cắt sớm theo `EMA50`** để tôn trọng cấu trúc SL sau đỉnh/đáy quét thanh khoản; `m15_intraday` kiểm tra cắt sớm theo **đúng nến `M15`**.
- **Hiệu quả backtest (`ict_intraday,m15_intraday`):**
  - **33 ngày (`24/10 – 26/11`):** `36` lệnh (`22W / 14L`), **WR `61.1%`**, MaxDD `$57.30`, **`$186.49` (`+$86.49`)**
  - **Tuần `16/11 – 21/11`:** `16` lệnh (`11W / 5L`), **WR `68.8%`**, MaxDD `$36.75`, **`$148.03` (`+$48.03`)**
  - **Tuần `23/11 – 26/11` (có tin USD):** `4` lệnh (`2W / 2L`), **WR `50.0%`**, MaxDD `$21.55`, **`$122.47` (`+$22.47`)**

---

## 2. Các Mode cũ đã dọn khỏi `STRATEGY_PRESETS` (tham số lưu trữ)

### A. `m5_scalp` *(đã thay bằng `ict_intraday`)*
- **Tham số từng dùng:** `timeframe="M5"`, `ema_fast=50`, `ema_slow=200`, `adx_min=15.0`, `sl_atr_mult=1.2`, `rr_ratio=1.7`, `use_session_filter=True` (`07–17 UTC`), `htf_timeframe="H1"`, `htf_ema_fast=20`, `htf_ema_slow=50`.
- **Kết quả đối chứng với `m15_intraday`:**
  - Bản `m5_scalp` (`ADX=0`, không lọc `H1`) chạy 33 ngày: `57` lệnh, WR `45.6%`, MaxDD `$90.91`, `$81.52` (`-$18.48`).
  - Bản `m5_scalp` thêm `ADX>=15 + H1`: `31` lệnh, WR `48.4%`, MaxDD `$40.81`, `$105.66` (`+$5.66`).
  - Khi chạy cặp `m5_scalp + m15_intraday` (lọc `H1`, dịch SL/TP theo spread): 33 ngày đạt `32` lệnh (`16W/16L`), WR `50.0%`, MaxDD `$31.28`, `+$29.14` — kém xa `ict_intraday + m15_intraday` (`+$86.49`, WR `61.1%`).

### B. `h1_swing`
- **Tham số:** `timeframe="H1"`, `ema_fast=50`, `ema_slow=200`, `adx_min=0.0`, `sl_atr_mult=2.5`, `rr_ratio=2.5`, `use_session_filter=True`.
- **Lý do bỏ:** Quá ít tín hiệu (`0` lệnh trong ~`65` ngày dữ liệu `H1` gần nhất trên Capital.com).

### C. `m15_active`
- **Tham số:** `timeframe="M15"`, `ema_fast=20`, `ema_slow=50`, `adx_min=18.0`, `sl_atr_mult=2.5`, `rr_ratio=2.0`, `use_session_filter=False` (chạy 24h).
- **Lý do bỏ:** Không lọc phiên nên dễ dính quét spread rạng sáng; đã thay bằng `m15_intraday` (`07–17 UTC`, `SL=1.0×ATR`, lọc `H1`).

---

## 3. Các biến thể ICT / SMC khác đã thử nghiệm (dữ liệu `24/10 – 26/11`)

| Biến thể | 33 ngày (`24/10–26/11`) | Tuần `16/11–21/11` | Tuần `23/11–26/11` | Ghi chú / Lý do không bật |
| :--- | :--- | :--- | :--- | :--- |
| **`ICT (FVG-Retest) + M15`** | `32` lệnh · WR `59.4%` · MaxDD `$60.08` · `+$93.14` | `13` lệnh · WR `76.9%` · `+$85.88` | `6` lệnh · WR `33.3%` · `-$0.67` | Dễ lỡ sóng chạy thẳng hoặc hồi lấp FVG rồi gãy (`-$11.16` ngày 26/11). |
| **`ICT (MSS+FVG) + ICT (FVG-Retest)` (`max_open=2`)** | `60` lệnh · WR `61.7%` · **MaxDD `$107.27`** · `+$193.87` | `27` lệnh · WR `77.8%` · `+$192.88` | `4` lệnh · WR `0.0%` · **`-$50.04`** | Nhồi 2 lệnh trùng 1 cú sweep/FVG → gặp tuần giật tụt nửa tài khoản `$100 → $49.96`. |
| **`ICT (MSS+FVG) + ICT (FVG-Retest)` (`max_open=1`)** | `39` lệnh · WR `53.8%` · MaxDD `$76.99` · `+$94.97` | `16` lệnh · WR `75.0%` · `+$58.91` | `5` lệnh · WR `0.0%` · **`-$52.40`** | Bị `MSS+FVG` chiếm slot lúc thắng và `Retest` vào bồi sau khi bị SL. |
| **SMC `M5`: `BOS+OB+PD + H1` (`07–17`)** | `19` lệnh · WR `52.6%` · MaxDD `$31.01` · `+$16.37` | `7` lệnh · WR `57.1%` · `+$19.72` | `3` lệnh · WR `33.3%` · `-$4.11` | Order Block dễ bị vàng quét râu xuyên qua trước khi chạy. |
| **SMC `M5`: `BOS+OB+IDM+PD + H1` (`07–17`)** | `5` lệnh · WR `40.0%` · MaxDD `$16.10` · `-$8.32` | `3` lệnh · WR `33.3%` · `-$0.44` | `0` lệnh (`$0`) | Thêm Inducement (IDM) khiến kèo quá hiếm (`5` lệnh/tháng) và vẫn âm. |
| **SMC `M5`: `BOS+OB+PD` (không `H1`)** | `33` lệnh · WR `33.3%` · MaxDD `$77.07` · `-$70.88` | `11` lệnh · WR `36.4%` · `-$8.93` | `6` lệnh · WR `33.3%` · `-$14.48` | Ngược xu hướng lớn nên gãy liên tục. |
| **SMC `M5`: `BOS+OB+PD + H1` (`24h`)** | `44` lệnh · WR `59.1%` · **MaxDD `$214.92`** · `+$430.94` | `11` lệnh · WR `72.7%` · `+$85.44` | `5` lệnh · WR `20.0%` · `-$23.80` | MaxDD quá lớn (`$214.92`), tuần gần nhất WR `20%`, chạy rạng sáng thực tế bị giãn spread. |
| **SMC `M15`: `BOS+OB+PD + H1` (`07–17`)** | `8` lệnh · WR `50.0%` · MaxDD `$28.55` · `+$39.19` | `2` lệnh · WR `50.0%` · `+$27.82` | `0` lệnh (`$0`) | Tần suất kèo rất thưa. |
