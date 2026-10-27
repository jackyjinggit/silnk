# 思印客 Silnk · PrintPilot

**校园文档交付终端：把「上传 → 检查 → 计价 → 支付 → 打印 → 取件 → 删除」做成一条可审计的链路。**

思印客不只是一台共享打印机。它关注三个结果：文件尽量不打错、纸张不被拿错、源文件不长期留在服务器和打印终端。

- 线上部署：`silnk.cn`（服务器正常，但该公网 IP 在大陆方向当前不可达，因此 demo 使用本地启动方式录制）
- 一页作品介绍：[docs/作品介绍_一页.html](docs/作品介绍_一页.html)（浏览器打开可直接打印 A4）
- 演示录屏脚本：[docs/演示录屏脚本.md](docs/演示录屏脚本.md)
- 完整功能与创新说明：[docs/产品功能与创新.md](docs/产品功能与创新.md)
- 硬件接入说明：[docs/打印机接入指南.md](docs/打印机接入指南.md)

## 核心能力

### 打印前：先检查，再计价

- 支持 PDF、Word、Excel、JPG、PNG；Word/Excel 通过 WPS 或 LibreOffice 转 PDF。
- 检查空白页、彩色页、页边距、低分辨率图片、混排尺寸与双面奇数页。
- 按论文、作业、实验报告等模板检查格式，支持一键修复、A3 拆 A4 和扫描件 OCR。
- 在本地文本层识别身份证号、手机号、银行卡号等敏感信息，支持一键打码；扫描件没有文字层时明确提示限制。
- 四档基础价、彩色覆盖率折扣、空白页不计费，前端预估与后端结算共用计价逻辑。

### 打印中：真实设备、订单隔离、进度可查

- Windows 客户端使用 SumatraPDF；Linux 工控机使用 CUPS，按队列提交并回报打印进度。
- 每台打印机使用独立设备标识或设备令牌；数据库条件更新配合队列锁，防止双客户端重复抢单。
- 支持预约出纸、班级批量姓名封皮、多人拼单和智能选机。
- 设备心跳、纸量、订单和 R1–R6 告警集中管理；出纸异常规则可检测设备在线但订单长期停在 `printing` 且没有出纸事件的情况。

### 打印后：取件和隐私都有记录

- 手机订单展示取件码；T5 屏幕在待取件状态显示大号取件码。
- 取件接口幂等记录时间、顺序与乱序信息，重复确认不会重复记取件。
- 上传会话过期、打印完成、用户主动删除等动作写入文件删除记录，管理端可审计。
- 预计完成时间、超时免单/退款待处理、格式不符重印申请，构成“交付承诺”，不把已提交打印命令等同于真实出纸。

## 系统组成

```text
手机 / 浏览器
  Vue 用户端 + 原生管理端 + 租户控制台
                  │ HTTPS
                  ▼
FastAPI + SQLite + APScheduler
  上传转换 · 印前检查 · 计价支付 · 订单队列 · 告警 · 删除审计
          │                         │
          ▼                         ▼
Windows / Linux 打印客户端       T5 串口桥接 ── UART ── T5AI + LCD
  心跳、取单、下载、出纸、回报       真实状态 / 纸量 / 取件码
          │
          ▼
CUPS / SumatraPDF + 打印机

RDK X5 + USB 摄像头 ── 出纸事件与现场图 ──> 后端 / 管理端
```

## 技术栈

| 部分 | 实现 |
| --- | --- |
| 后端 | Python、FastAPI、SQLite WAL、APScheduler |
| 用户端 | Vue 3、Vite、Element Plus |
| 管理端与租户端 | 原生 HTML/CSS/JavaScript |
| 文档处理 | PyMuPDF、pypdf、LibreOffice/WPS、Tesseract（可选） |
| 打印客户端 | Windows SumatraPDF；Linux CUPS、ipptool |
| T5 取件屏 | TuyaOpen、C、LVGL、UART 逐行 JSON |
| 出纸检测 | RDK X5、OpenCV、USB 摄像头 |
| 测试 | pytest，含接口、计价、权限、告警、并发和检测逻辑测试 |

## 本地运行

### 环境要求

- Python 3.11 或更新版本（本项目开发环境为 Python 3.12）。
- Node.js 20 或更新版本，仅在重新构建前端时需要。
- Windows 可直接体验 Web 与本地接口；实际打印还需要打印机客户端及相应驱动。

### 1. 启动后端

在项目根目录执行：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/>。接口调试页面为 <http://127.0.0.1:8000/docs>，管理端为 <http://127.0.0.1:8000/admin/>。

首次启动会创建本地 SQLite 数据库及运行时目录。`SECRET_KEY` 未设置时使用仅供本地开发的默认值；**公网部署必须通过环境变量设置独立强密钥**。

本地需要短信验证码演示时，可显式设置：

```bash
# PowerShell
$env:SMS_DEMO_MODE = "1"
```

此模式会在页面标注并返回演示验证码，不会假装真实短信已发送；公网环境不要开启。

### 2. 重新构建用户端

```bash
cd front
npm ci
npm run build
```

构建结果输出到 `static/app/dist`，由 FastAPI 同源托管。仓库已包含构建产物，因此只启动后端时不必先安装 Node 依赖。

## 连接实际打印机

### Windows

1. 安装打印机驱动和 SumatraPDF。
2. 复制 `client/start_win.bat.example` 为 `client/start_win.bat`。
3. 填写服务器地址、设备密钥、打印机队列名和设备 ID。
4. 在 `client` 目录运行该脚本。

`client/config.json` 和实际启动脚本均不提交，避免泄露设备令牌。

### Linux 工控机

先安装 CUPS、打印驱动及 IPP 工具，确认队列真实可用：

```bash
sudo apt-get install -y cups cups-client cups-ipp-utils python3
lpstat -p
```

然后配置 `SERVER_HOST`、`SECRET_KEY`、`PRINTER_URL`、`PRINTER_DEVICE_ID`，运行 `python3 client/main.py`。完整示例见 [打印机接入指南](docs/打印机接入指南.md)。

> CUPS 或 `lp` 返回成功只代表任务被接受，不必然证明纸张已经输出。真实出纸需结合现场观察或 X5 出纸事件验证。

## T5AI 取件屏

提交包在 `hardware/t5ai/` 提供 TuyaOpen 应用覆盖文件，包含实际使用的 LVGL 界面和子集字体；不包含整个 TuyaOpen SDK。

```text
后端状态接口 → client/display/t5ai_poll.py → UART0 115200 → T5 LVGL 屏幕
T5 点击确认  → UART JSON 回行 → 桥接 → 取件接口 → 结果回写屏幕
```

屏幕区分“等待首帧 / 在线 / 离线”三种链路状态；超过 6 秒没有完整帧会置灰并保留最后内容。纸量或单价未知时显示 `--`，不把旧值当成新数据。

构建方式与串口协议见 [hardware/t5ai/README.md](hardware/t5ai/README.md)。桥接密钥从 `PRINTER_KEY` 或 `SECRET_KEY` 环境变量读取，Windows 可用 `client/display/start_t5.bat` 启动。

## RDK X5 出纸检测

`client/edge/x5_eject.py` 在摄像头 ROI 内结合亮度、帧差、稳定帧和冷却时间判断出纸，并将事件与现场照片绑定到当前活动订单。

```bash
pip install -r client/edge/requirements.txt
python client/edge/x5_eject.py --help
```

摄像头应固定并对准出纸区域，先建立空托盘基准再启用。光照变化和机位移动可能影响检测结果。

## 测试与验证边界

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

- 自动化测试覆盖后端接口、权限、计价、告警、并发抢单、出纸事件及检测算法；测试通过不等于所有硬件已验收。
- T5 固件已完成编译和烧录，并接收过线上真实 idle 状态、排队、纸量与单价；取件态点击闭环和长期稳定性仍需现场复核。
- X5 已具备事件和照片链路；现场需重新建立摄像头基准并验证光照、机位与重复事件。
- 真实短信、微信/支付宝授权登录、自动支付分账未接入。支付与退款状态按代码实际能力区分，不宣称已自动到账。
- 内容审阅支持本地隐私规则和可选的 DeepSeek/GLM 通道。**配置第三方模型后，文本或页面图像可能发送至相应服务商**，不能宣称所有文档处理都只在本机完成。

## 目录

```text
main.py / routers/       FastAPI 入口与业务接口
db.py / alerts.py        SQLite 数据访问、R1–R6 告警
pricing.py / pii.py      统一计价、本地敏感信息识别
front/                   Vue 用户端源码
static/                  用户端构建产物、管理端、租户端与介绍页
client/                  Windows/Linux 打印客户端、T5 桥接、X5 检测
hardware/t5ai/           T5AI LVGL 固件覆盖源码
convert/ / templates/    文档转换与格式模板
tests/                   自动化测试
docs/                    功能、接入、测试和项目过程文档
```

## 安全与来源

提交包不包含 `.git`、数据库、上传文件、打印缓存、备份、收款码、设备配置、前端依赖或真实密钥。部署前请配置 `SECRET_KEY`，运维端另配 `OPS_KEY`；所有外部服务密钥均通过环境变量注入。

本项目在 [233kun/self-service-printer](https://github.com/233kun/self-service-printer) 的基础上重构与扩展。T5 应用基于 [TuyaOpen](https://github.com/tuya/TuyaOpen) 示例环境开发，遵循其 Apache-2.0 许可及上游声明。
