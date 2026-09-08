# 故障排查决策树

按当前失败阶段往下走。每个 Case 给出"现象 → 最可能原因 → 动作"。

## 1 configure 阶段(CMake 配置失败)

打开 `configure.log` 看第一处 ERROR:

- **找不到 arm-none-eabi-gcc** → 工具链 bin 不在 PATH。find_tools.py 能找到 GCC 就说明 devloop
  已自动注入 PATH;若 GCC 本身缺失,装 STM32CubeCLT 或 Cube VSCode 扩展的 gnu-tools bundle。
- **Unknown CMake command / presets 版本报错** → 系统 cmake 太老,指定 bundles 里的新版
  (devloop 默认就这么干;手动跑命令时用 find_tools 输出的那个 cmake.exe)。
- **toolchain file not found** → 相对路径断了,检查工程里 `cmake/*.cmake` 是否被挪动。

## 2 build 阶段(GCC/Ninja)

`DEVLOOP_RESULT.artifacts.errors[]` 第一条就是锚点:

| 报错关键字 | 真实原因 | 对策 |
|---|---|---|
| `undefined reference to 'xxx'` | 符号所在 .c 没参与编译 / 只声明未实现 | 把源文件加入 CMakeLists 的 target_sources;或补实现 |
| `multiple definition of ...` | 同一符号两个翻译单元都有定义(常用错:头文件里定义全局变量) | 定义挪 .c,头文件留 extern |
| `region 'FLASH' overflowed` / `will not fit in region` | 固件比 Flash 大(开了大数组/字体表/调试信息宏) | 看 .map 里 Section 名找大户;裁功能或换更大容量型号 |
| `cannot find -lm` 等 | 工具链 specs 不全 | 用完整版工具链(bundles 自带的没问题) |
| `%f` 打印为空/程序行为异常但无警告 | newlib-nano 默认不带浮点格式化 | 链接参数加 `-u _printf_float` |
| 改了文件却没生效 | Ninja 认为没变化(ioc 重新生成时间戳问题) | 删掉对应 .o 或全量重配 |

## 3 flash 阶段(烧录)

`flash.log` 全文 + RESULT.hint 对照:

- **No ST-LINK detected** → USB 线/口,stlink-usb-driver 驱动;个别台式机前面板供电不足换后置。
- **TARGET NO SYNC / Cannot connect**(mode=UR 也救不了)→ 依次怀疑:
  ①板子供电 ②PA13/PA14 被程序改作他用(上电就禁了 SWD——解决方案:Boot0 拉 VCC 进系统bootloader,
  或量产前保留"上电延迟 100ms 再初始化该引脚") ③外部复位电路问题。
- **Read protection / MEM_LEVEL error** → 芯片开着 RDP:全片擦除解锁(会清程序):
  `STM32_Programmer_CLI.exe -c port=SWD mode=UR -e all`,再正常烧录。
- **Download verified successfully 但用户说"板子没动静"** → 固件根本没跑到业务逻辑,跳转第 4 节。
- **反复第一页擦除失败/Wrong signature** → 个别兼容 CLONE ST-Link 或线太长降速:CLI 里加
  `freq=960` 试试(SWD 降到 ~1MHz)。

## 4 capture 阶段

### 4a. 打不开端口(Access is denied)
COM 口是独占资源:VSCode Serial Monitor(底部状态栏常驻最爱犯)、PuTTY/MobaXterm、残留 python
进程。serial_capture.py 已经会把常见嫌疑人念给你听;都不在就让用户看一眼托盘。

### 4b. 端口开了,0 字节(静默树)
按顺序问四个问题,全部可在一次迭代内验证:

1. **固件里有能在开机阶段输出的日志点吗?** 若 main 里最早的 printf 在传感器初始化之后,传感器挂了
   自然全程安静。正确姿势:main() 第一行就打一行 boot banner,任何死法都能在串口上划出"死前痕迹"。
2. **波特率对吗?** 时钟树(ioc 的 HSE 值)被改过而 USART 分频沿用旧值 → 波特率漂移。表现为乱码而非
   静默,但低速率差一位会像半静默。核对 SystemCoreClock 与卡片记载值。
3. **是不是启动早期就死了(clock/HSE 断言、SystemInit 阶段)?** 特征:一颗 LED 都不闪。
   验证手段(按侵入性从低到高):
   - SWD 还能连上 ⇒ 用 flash.py 的 CLI 挂 gdbserver 附带能力之外,更快的土办法:临时在 Reset_Handler
     后第一句插 GPIO 置高一个空引脚 + while(1),量得到电平 = 死点在 main 之后;
   - 复位瞬间能不能听见继电器声之类的物理响应(搞笑但有效);
   - 终极:回滚到上一个确定能跑的 commit 二分定位。
4. **跑到一半才停** → HardFault(除零/非法指针/栈溢出):把栈加大一档重烧最快验证;能坚持到 printf
   说明不死在调度期,检查 watch/delay 逻辑。

### 4c. 有数据但是乱码
9 成波特率不匹配(见上)。剩下 1 成:电平(TTL↔RS232 共地)或者晶振与 ioc 不符
(8MHz 板贴了 12MHz 图纸抄错)。

### 4d. 只有上半段输出
缓冲问题:确认 `__io_putchar` 后加 `_write` 重定向更稳(newlib 将 printf 组包到整行,轮询发送也无妨)。

## 5 别忘了往事实卡里记一笔

本次踩的新坑若属于"这块板子/这个工程下次还会遇到"级别(比如某个外设复用冲突、CubeMX 重新生成会覆盖
的手工修改),把它追加到工程根目录 `debug-loop.md` 的 quirks 小节。这正是这个闭环越用越顺的原因。
