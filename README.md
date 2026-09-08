# stm32-debug-loop

此 Skill 面向 Claude Code，为 STM32 固件开发提供「编译 → ST-Link 烧录 → 串口捕获验证」的自动化闭环。

它将开发中最频繁重复的几步操作串联为一条流水线，全程无需人工值守。Agent 只需执行一条命令，
即可从生成的日志中判断固件是否运行符合预期。

适用范围：Windows 10/11；任意基于 CubeMX + GCC + CMake 的工程；开发板经 ST-Link 连接，调试串口以 printf 输出。
日常场景中的「编译烧录一下」「烧完没有反应」「串口无声/乱码」「修改后上板验证」等操作均由此 Skill 承担。

> 说明：GDB 断点单步等需要人工实时观察的交互式调试，不在本 Skill 的能力范围内。

## 设计背景

AI Agent 无法观察交互式终端。让 Agent 驻留在串口助手或 read 循环中等待数据，既不可行也难以解读。
因此本 Skill 采取如下策略：**将串口输出按时间窗口捕获为一个文件**，Agent 再以读取普通日志的方式
`Read` 该文件，据此取证并作出判断。整套工具即围绕这一机制构建。

## 目录结构

```
stm32-debug-loop/
├── SKILL.md                        # Skill 主体，由 Claude Code 加载
├── scripts/
│   ├── find_tools.py               # 定位 cmake / ninja / GCC / Programmer_CLI，输出 JSON
│   ├── devloop.py                  # 一站式：configure → build → flash → capture
│   ├── flash.py                    # 仅烧录，将失败原因转换为可读说明
│   └── serial_capture.py           # 仅捕获串口 N 秒并写入 UTF-8 文件
├── references/
│   ├── troubleshooting.md          # 按失败阶段划分的排查决策树
│   └── project-facts.template.md   # 每个工程一份「事实卡」的模板
└── evals/
    └── evals.json                  # 两组端到端评估任务
```

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10/11 |
| Python | ≥ 3.10，需安装 `pyserial` |
| 烧录工具 | STM32CubeProgrammer CLI（来自 STM32CubeCLT 或 STM32Cube VSCode 扩展） |
| 固件工程 | 含 `CMakePresets.json`，基于 CMake + Ninja + arm-none-eabi-gcc |
| 硬件 | ST-Link 调试器与串口连接 |

若工具定位失败，脚本会依次检索「VSCode bundles → `C:\ST\STM32CubeCLT` → 系统 PATH」。

## 脚本说明

下方 `$S` 指本工程 `scripts/` 的绝对路径。

| 脚本 | 功能 | 调用示例 |
|---|---|---|
| `find_tools.py` | 检测工具链是否就绪 | `python "$S/find_tools.py"` |
| `devloop.py` | **主入口**，完成全部四步 | `python "$S/devloop.py" --project <工程目录> --seconds 4` |
| `flash.py` | 仅烧录 | `python "$S/flash.py" --elf xxx.elf` |
| `serial_capture.py` | 仅捕获串口 | `python "$S/serial_capture.py" --out uart.log --seconds 4 --ts` |

### devloop.py 的使用

```bash
python "$S/devloop.py" --project D:/path/to/project --seconds 4 --ts
```

执行结束后输出一行 `=== DEVLOOP_RESULT === {...}`，表明流程终止于哪一阶段：

- `stage: build` 且含 `errors[]` → 每条错误均带 `file:line`，建议**先修复第一条再重跑**，避免连锁误判。
  出现 `undefined reference` 多因某源文件未加入 CMakeLists。
- `stage: flash` 含 `hint` → 按其提示处理（SWD 未接触、以 mode=UR 复位下连、读保护 RDP 等）。
- `stage: capture` 且 `silent: true` → 端口已打开但无任何数据，应转至排查章节。
- 成功 → 打开 `uart.log`（路径见 artifacts），核实内容后决定是否继续迭代。

每次执行的完整日志分别写入 `<工程>/build/devloop_last/` 下的 `build.log`、`flash.log`、`uart.log`，
另附机器可读的 `result.json`。

如需只执行其中一段：编译查错使用 `--no-flash --no-capture`；烧录后欲延长观察时间，单独调用
`serial_capture.py`，其结束时自动释放端口。

## 与目标工程的关系

本 Skill 为通用工具，不依赖特定工程，但依赖每个工程维护一份「事实卡」，记录串口号/VID:PID、
波特率、开机预期输出及已知问题（quirks）。检索顺序如下：

1. `docs/debug-loop.md`
2. 工程根目录的 `debug-loop.md`
3. `README.md` 中的调试章节
4. `bsp/boards/<板型>/board.h`

若检索命中则照其执行；否则依据 `.ioc`/README/原理图推断，与用户确认后**代为创建**一份。
模板见 `references/project-facts.template.md`。

## 故障排查

任一步骤失败时，请先查阅 [references/troubleshooting.md](references/troubleshooting.md) 对应小节。
该文档按阶段提供了决策树，涵盖烧录失败分类、串口静默排查、乱码定位及常见 GCC 报错对照。

## 操作注意事项

以下事项均源自实际项目经验：

- **COM 口与 ST-Link 均为独占资源。** 开始新一轮前，须确认无其他进程占用串口——最常见为 VSCode 底部状态栏的 Serial Monitor 未关闭，或先前 GDB 会话仍驻留；否则 Capture 阶段将报 Access denied。
- **DTR/RTS 默认不触发。** 多数开发板将复位接于 DTR，默认值已属安全；除非事实卡另有约定，不建议添加 `--dtr`。
- **结论须附证据。** 汇报时应引用 `uart.log` 尾部的关键行，而非仅作定性描述。
- **烧录等待保留余量。** devloop 内部已设超时（build 15 分钟、flash 3 分钟），整体未结束前请勿重复启动。
- **`%f` 无输出**，多半为 newlib-nano 未包含浮点格式化，链接时添加 `-u _printf_float` 即可。

## 验证方式

[evals/evals.json](evals/evals.json) 定义了两组端到端任务，均需连接真实开发板执行：

| id | 场景 | 验证目标 |
|---|---|---|
| 0 | `add-led-blink-counter` | 实现 LED 闪烁与计数打印，烧录后捕获严格递增的 `blink=N` |
| 1 | `diagnose-boot-fault` | 定位静默故障根因（早期 HardFault），修复后重烧恢复开机输出 |

## 安装说明

本目录本身即为一处 Skill，置于 `~/.claude/skills/stm32-debug-loop/`。其触发依据为 `SKILL.md`
frontmatter 中的 `description` 字段，故该项已对常见措辞（如「编译烧录一下」「上板验证」）作了充分覆盖，
以确保语义相符的会话能够命中。

## 持续改进

若在某块板卡或某工程中发现了具有反复性的问题，建议将其追加至该工程根目录 `debug-loop.md` 的
quirks 小节。每一次记录都将使后续会话免于重复踩坑，这也是本闭环长期价值所在。
