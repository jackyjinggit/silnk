# T5AI 取件屏（TuyaOpen + LVGL）

思印客的自助取件屏：接后端真实状态，显示排队人数、剩余纸张、单价与取件码，并支持在待取件状态点击屏幕确认取件。

本目录是 TuyaOpen 应用覆盖源码，**不含整个 TuyaOpen SDK**。上游 SDK 需自行克隆，本仓库只提供实际在用的界面与字体。

## 1. 硬件

| 项 | 型号 |
| --- | --- |
| 开发板 | 涂鸦 T5AI-Board（T5-E1-IPEX） |
| 屏幕 | 3.5 寸 LCD 触摸屏（`CONFIG_TUYA_T5AI_BOARD_LCD_35565=y`） |
| 串口 | UART0，115200 8N1，逐行 JSON |

板子通过 USB 接工控机，由 `client/display/t5ai_poll.py` 把后端数据推到串口。

## 2. 串口协议

### 上位机 → 屏幕

每帧一行紧凑 JSON，`\n` 结尾：

```json
{"s":"idle","q":0,"c":"","t":1792853742,"p":232,"u":35}
```

| 字段 | 含义 |
| --- | --- |
| `s` | 状态：`idle` 可打印 / `busy` 打印中 / `pickup` 待取件 |
| `q` | 排队人数，未知为 -1 |
| `c` | 取件码（仅 `pickup` 态有值） |
| `t` | 后端时间戳（秒） |
| `p` | 剩余纸张，未知为 -1 |
| `u` | 黑白单价，单位「分」，未知为 -1 |

`p` / `u` 为 -1 时屏幕显示 `--`，不沿用上一帧的数字。

**超时策略**：超过 6 秒没有收到完整帧，界面置灰并保留最后一帧内容，顶部状态从「在线」变「离线」；首帧未到时显示「连接中」。

### 屏幕 → 上位机

待取件状态点击主卡片时发送：

```json
{"a":"pick","c":"5218"}
```

上位机调用后端取件接口后回写结果：

```json
{"r":"pick","ok":1,"m":"已取件 请拿好纸张"}
```

| `ok` | 屏幕表现 |
| --- | --- |
| 1 | 提示成功文案，随后回到「可打印」 |
| 0 | 提示失败原因（如「取件码已失效」），允许立即再点 |

板子只做展示，不做业务判断。

## 3. 编译与烧录

### 3.1 准备源码

```bash
git clone https://github.com/tuya/TuyaOpen.git
```

把本目录整体放到：

```text
TuyaOpen/apps/games/lvgl_games/
├── CMakeLists.txt
├── app_default.config
├── config/TUYA_T5AI_BOARD_LCD_3.5.config
└── src/
    ├── lv_games.c      界面与串口逻辑
    ├── lv_games.h      LVGL v8/v9 兼容宏
    ├── sy_title.c      标题字体
    ├── sy_state.c      大号状态字
    ├── sy_ui.c         小号信息字
    ├── sy_tiny.c       角标字
    ├── sy_num_l.c      取件码大数字
    ├── sy_num_m.c      磁贴数字
    └── sy_black.c      辅助字形
```

### 3.2 修改 SDK 颜色深度

`TUYA_T5AI_BOARD_LCD_3.5.config` 已指定 `CONFIG_LVGL_COLOR_DEPTH=32`。若使用 v9 分支的配置文件，同时确认 `TuyaOpen/src/liblvgl/v9/conf/lv_conf.h` 中：

```c
#define LV_COLOR_DEPTH 32
```

高于 16 位的图片数据在 16 位深度下会显示异常。

### 3.3 编译

```powershell
.\export.ps1
cd apps\games\lvgl_games
python ..\..\tos.py build
```

产物：`.build/bin/lvgl_games_QIO_1.0.0.bin`

若提示 `Update the platform to the required commit?`，确认平台版本后再继续；不要在未确认的情况下把 SDK 平台提交切走。

### 3.4 烧录

先关闭占用串口的桥接进程，再烧录：

```powershell
python ..\..\tos.py flash -p COM4 -b 921600
```

烧录成功后，桥接会用同一 `COM4` 推送数据。

## 4. 字体与乱码处理

界面里的中文字体是 **子集字体**，只包含生成时指定的字符。新增中文文案后如果忘记重新生成字体，屏幕会显示空白或乱码（本轮新增「等待连接」「连接中断」时曾因缺字形出现花屏，补齐字符并重烧后恢复）。

新增按钮、提示语后，必须用 `lv_font_conv` 重新生成对应字体，并把新文案里的所有汉字加进 `--symbols`。例如：

```bash
lv_font_conv --font "C:\Windows\Fonts\Noto Sans SC Bold (TrueType).otf" \
  --size 22 --bpp 4 --no-compress --format lvgl --lv-include lvgl.h \
  -r 0x20-0x7E --symbols "欢迎使用自助打印正在出纸请稍候轻触屏幕确认取件..." \
  -o sy_ui.c
```

各字体的尺寸与用途：

| 文件 | 字号 | 用途 |
| --- | --- | --- |
| `sy_title.c` | 面向标题 | 「思印客」 |
| `sy_state.c` | 大号 | 可打印 / 打印中 / 请取件 |
| `sy_ui.c` | 22 px | 副标题、提示语、单位 |
| `sy_tiny.c` | 小号 | 角标、英文说明 |
| `sy_num_l.c` | 76 px | 取件码数字 |
| `sy_num_m.c` | 中号 | 排队与纸量磁贴数字 |

## 5. 运行桥接

```powershell
cd self-service-printer-master\client\display
$env:PRINTER_KEY = "设备密钥"
.\start_t5.bat
```

或直接：

```bash
python -u client/display/t5ai_poll.py \
  --device printer-001 --port COM4 --backend https://silnk.cn
```

密钥也可用 `--key` 传入。取数接口为 `GET /api/display/{device_id}/state`；旧后端没有该接口时会降级为「单价 + 设备详情」，此时拿不到取件码，只能区分可打印与打印中。

## 6. 已验证与未验证

已验证：

- 固件编译通过、烧录成功（COM4，115200）。
- 桥接已对接线上接口并持续下发真实帧，例如 `{"s":"idle","q":0,"p":232,"u":35}`；中文显示正常，无乱码。
- 6 秒无帧置灰、保留末帧；等待首帧显示「连接中」的逻辑已实现。

尚未验证，需现场复核：

- 真实 `pickup` 态的取件码显示与点击闭环（后端需要一张真实待取件订单）。
- 连续 10 分钟运行是否死机、花屏或数字跳变。
- 语音叫号（当前固件未实现）。

上游说明见 `TuyaOpen/apps/games/lvgl_games/README.md`；游戏相关模块在本应用已全部关闭，只保留取件屏界面。
