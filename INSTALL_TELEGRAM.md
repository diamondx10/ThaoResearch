# Hướng dẫn cài đặt Telegram Bot cho ThaoResearch

Tài liệu này hướng dẫn tạo Telegram Bot và lấy Chat ID để gửi cảnh báo từ ThaoResearch.

> **CẢNH BÁO: Không bao giờ commit Bot Token thật vào git.**
> Nếu vô tình để lộ token, ngay lập tức gửi `/revoke` cho @BotFather để tạo token mới.

---

## Phần 1: Tạo bot với BotFather

1. Mở ứng dụng Telegram, tìm kiếm `@BotFather`.
2. Gửi lệnh `/newbot`.
3. BotFather sẽ hỏi:
   - **Bot name**: `ThaoResearch Alerts` (hoặc tên tuỳ thích)
   - **Bot username**: phải kết thúc bằng `bot`, ví dụ `thao_research_alerts_bot`
4. BotFather trả lời với token có dạng:
   ```
   123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   ```
   Lưu token này lại.

---

## Phần 2: Lấy Chat ID cá nhân

1. Trong Telegram, tìm kiếm `@userinfobot` hoặc `@RawDataBot`.
2. Gửi bất kỳ tin nhắn nào cho bot đó.
3. Bot trả lời với thông tin của bạn, tìm dòng `Id: 987654321` — đó là Chat ID cá nhân của bạn.

---

## Phần 3: Lấy Chat ID nhóm

1. Thêm bot của bạn vào nhóm Telegram.
2. Gửi một tin nhắn bất kỳ trong nhóm.
3. Mở trình duyệt, truy cập:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Thay `<YOUR_TOKEN>` bằng token đã lưu ở Phần 1.
4. Trong response JSON, tìm `"chat":{"id": -100XXXXXXXXXX}` — đó là Chat ID nhóm (số âm).

---

## Phần 4: Cấu hình `.env`

Mở file `.env` trong thư mục gốc của ThaoResearch, điền:

```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=987654321
```

Nếu dùng nhóm, `TELEGRAM_CHAT_ID` là số âm, ví dụ `-1001234567890`.

---

## Phần 5: Test kết nối

Sau khi lưu `.env`, chạy:

```
python main.py --test-telegram
```

Kết quả mong đợi:
```
Testing Telegram connection...
[OK] Bot @thao_research_alerts_bot connected to chat 987***
```

Nếu thất bại, xem Phần 6 để xử lý.

---

## Phần 6: Troubleshooting

### Lỗi 401 Unauthorized

```
Telegram rejected the bot token (401 Unauthorized)
```

- Bot Token sai hoặc đã bị thu hồi.
- Kiểm tra lại giá trị trong `.env` (copy nguyên văn từ BotFather, không thêm khoảng trắng).
- Nếu nghi ngờ bị lộ: gửi `/revoke` cho BotFather rồi lấy token mới.

### Lỗi 403 Forbidden

```
Bot was blocked by the user (403 Forbidden)
```

- User đã chặn bot. Dùng Chat ID khác hoặc yêu cầu user unblock.

### Lỗi Chat not found

```
Chat ID -100123456789 not found
```

- Bot chưa được thêm vào nhóm, hoặc Chat ID sai.
- Với cá nhân: kiểm tra user đã từng nhắn với bot (gửi `/start` cho bot trước).
- Với nhóm: đảm bảo bot đã được thêm và có quyền gửi tin nhắn.

### Lỗi 400 Bad Request

```
Telegram rejected message format
```

- Title chứa ký tự Markdown đặc biệt bị escape sai.
- Đã có xử lý tự động; nếu vẫn lỗi, kiểm tra logs/app.log.

### Lỗi 429 Too Many Requests

```
Telegram rate limit
```

- Tool tự động đợi theo `retry_after` rồi gửi lại.
- Không cần làm gì.

### Bot không gửi được vào nhóm

- Trong nhóm Telegram, kiểm tra quyền của bot.
- Nếu nhóm bật "Restrict saving content" → có thể chặn bot gửi.

### Rate limit Telegram

- Tần suất tối đa: ~30 message/giây/cùng bot, ~1 message/giây/cùng chat.
- ThaoResearch chỉ gửi khi có alert thật → thường < 10 message/ngày, không lo.

---

## Phần 7: Bảo mật

- **KHÔNG** commit `.env` lên git (đã có trong `.gitignore`).
- **KHÔNG** paste token vào issue/discord/forum public.
- Nếu lỡ commit nhầm: dùng `git filter-repo` hoặc BFG Repo-Cleaner để xoá khỏi lịch sử.
- Khi test với CI, inject token qua environment variable, không ghi vào file.

Token Telegram là mật khẩu — bất kỳ ai có token đều có thể điều khiển bot của bạn hoàn toàn.
