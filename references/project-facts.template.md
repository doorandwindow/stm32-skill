<!--
使用方法:复制本模板到目标工程根目录,命名 debug-loop.md,删掉注释并填好每一项。
这张卡是 stm32-debug-loop skill 的输入:脚本参数几乎全部由它驱动;quirks 字段请在每次
排除疑难后追加,让下一次会话不必重新踩坑。
-->

# Debug Loop 事实卡:<工程名>

- **MCU / 板卡**:STM32F407ZGT6 @ ALIENTEK 探索者(示例)
- **构建**:preset=`Debug`,产物 `build/Debug/<名>.elf`
- **调试器**:ST-Link V2,SWD(PA13=SWDIO / PA14=SWCLK)

## 串口(取证通道)

| 项 | 值 |
|---|---|
| 物理链路 | 板载 CH340 ↔ MCU USART1(PA9=TX) |
| 定位方式 | VID:PID = `1A86:7523`(推荐;COM 号会漂移) |
| 当前 COM 号 | COM18(**以实际枚举为准**) |
| 波特率 | 115200 |
| DTR 注意 | 板子不复位型,默认即可 |

## 开机预期输出(判断"活着")

```
[boot] SystemCoreClock=168000000
LCD ID=9341 ; MPU6050 init OK      ← 第一条业务日志之前应看到的基线
```

## quirks(已知坑,按日期倒序追加)

- 2026-08-27 `delay_init(168)` 接管 SysTick 后 HAL_Delay/HAL_GetTick 刻度失真 8×,
  新增代码一律用正点原子 delay_ms/us。
- 2026-08-27 `Core/Src/fsmc.c` 的 FSMC 时序调优在 USER CODE 区外,CubeMX 重新生成代码会被
  覆盖回保守值——重新生成后必须复查 ASET=2/HOLD=1/DSET=6。
- ……
