# 微雪 ESP32 设备信息

记录日期：2026-10-05。硬件规格依据微雪官方资料；连接状态来自本次本机及路由器检查，后续使用前需重新确认。

## 已确认的设备

用户提供 SKU **33691**，对应 **Waveshare ESP32-S3-Touch-AMOLED-1.75C**。注意名称末尾的 **C**，下载驱动、示例和固件时按完整型号匹配。

| 项目 | 规格 |
|---|---|
| 主控 | ESP32-S3R8，Xtensa LX7 双核 32 位，最高 240 MHz |
| 片内存储 | 512 KB SRAM、384 KB ROM |
| 扩展 RAM | 8 MB PSRAM |
| Flash | 32 MB NOR Flash，以当前官方产品文档为准；尚未通过芯片读取核实实物容量 |
| 显示 | 1.75 英寸 AMOLED，466 × 466，约 1670 万色 |
| 显示驱动 | CO5300，QSPI |
| 触摸 | CST9217 电容触控，I²C |
| Wi-Fi | 2.4 GHz，802.11 b/g/n，板载天线；不支持 5 GHz Wi-Fi |
| 蓝牙 | Bluetooth 5 LE |
| 音频输入 | 双麦克风、ES7210 音频采集芯片，配合回声消除方案 |
| 音频输出 | ES8311 音频编解码芯片、内置扬声器 |
| 运动传感器 | QMI8658，三轴加速度计＋三轴陀螺仪 |
| 电源管理 | AXP2101 |
| 电池接口 | 3.7 V 锂电池，MX1.25 2PIN，支持充放电；实物是否安装电池未检查 |
| 接口和按键 | USB Type-C；PWR、BOOT |
| 外壳 | CNC 铝合金 |

来源：[微雪官方产品文档](https://docs.waveshare.net/ESP32-S3-Touch-AMOLED-1.75C/)。

## 本次识别信息

| 项目 | 2026-10-05 观察结果 |
|---|---|
| USB 厂商 | Espressif |
| USB 设备名称 | USB JTAG/serial debug unit |
| USB VID:PID | 303A:1001 |
| USB 序列号／路由器中对应 MAC | 7C:0C:5F:42:4B:FC |
| macOS 串口 | `/dev/cu.usbmodem2101`，重插或换 USB 口后可能变化 |
| 路由器 | GL.iNet GL-BE6500，管理地址 `http://192.168.8.1` |
| 最后观察到的 DHCP 地址 | `192.168.8.148`；日志时间 21:59:58，地址不是永久保证 |

用户提到两台 ESP32。路由器中还观察到 MAC `44:BD:8D:78:14:5C`、地址 `192.168.8.184`、名称 `espressif` 的设备。**第二台型号、SKU、USB 串口及是否确为用户所说的第二台尚未核实**，不能直接套用上面的硬件规格。

## 日志及问题记录

- USB 日志：用 Python 的 `os.open` 以 `O_RDONLY | O_NONBLOCK | O_NOCTTY` 打开串口，再用 `select` 有限时监听。未发送串口命令、刷机或主动复位。静默不能证明设备没有故障；也可能读到缓冲中的旧消息。
- 路由器日志：用户自行登录后，从 **SYSTEM → Log → System Log** 读取，以 MAC 和时间关联事件。
- 微雪设备曾多次 Wi-Fi 握手后断开，后来完成握手并获 DHCP 地址。拿到 IP 不等于能访问互联网。
- 用户报告在线版本更新错误 `32774`。尚未捕获对应的完整设备更新错误日志，因此未最终定位根因。
- 本机测试 `https://api.tenclass.net:8980/xiaozhi/ota/` 超时；不带 `:8980` 的地址返回 HTTP 200 和 `status: ok`。这是当时电脑网络路径的结果，不能代替设备端验证，也不能证明已完成 OTA。
- 没有修改路由器设置，也没有执行设备擦除或刷机。**远程熄屏未完成**：当前未发现可用控制接口；访问设备当时的 HTTP 地址连接被拒绝。

## 自行开发与烧录

**USB 烧录不要求开发板先配网、激活小智或成功进行 OTA。** 电脑需要先准备开发工具和依赖；程序是否联网取决于应用设计。

- Arduino：官方提供显示、LVGL、QMI8658、ES7210 和 ES8311 示例，适合先验证屏幕和音频。
- ESP-IDF：官方提供电源、LVGL、Brookesia、运动交互及音频频谱示例，可作为自行开发的基础。
- 小智官方云端使用流程需要互联网和 2.4 GHz 配网。自写离线程序不需要小智服务；自写 HA 终端可按自己的局域网通信方案实现。
- 小智固件与演示程序是独立程序，通常通过重新烧录切换。覆盖前可备份现有 Flash。
- 微雪出厂测试合并固件的官方烧录地址是 `0x00`；**不要把这个地址套用到任意应用单独的 `.bin` 文件**，自己的工程以构建产物的烧录参数为准。
- 无法进入下载时，官方方法是按住 BOOT 再重新上电。完成后退出下载模式并正常重启。

官方资料：

- [Arduino 开发](https://docs.waveshare.net/ESP32-S3-Touch-AMOLED-1.75C/Arduino/)
- [ESP-IDF 开发](https://docs.waveshare.net/ESP32-S3-Touch-AMOLED-1.75C/ESP-IDF/)
- [小智配网、开发与常见问题](https://docs.waveshare.net/ESP32-S3-Touch-AMOLED-1.75C/XiaoZhi_AI/)
- [出厂固件烧录与擦除](https://docs.waveshare.net/ESP32-S3-Touch-AMOLED-1.75C/Firmware-Flashing/)
- [微雪官方源码与示例](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-1.75C)

官方网页、示例及版本可能更新。选择与完整板型匹配的发布包，并核对该包要求的工具链、引脚及依赖，不将搜索缓存或其他尺寸板子的参数当成实物检测结果。
