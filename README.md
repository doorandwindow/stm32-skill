# stm32-debug-loop

> 一个 Claude Code **skill**：STM32 固件「编译 → ST-Link 烧录 → 串口捕获验证」的自动化闭环工具箱。
> 平台：Windows 10/11。通用适用于任何 CubeMX / GCC + CMake 的固件工程。

凡是要把固件 build 出来、烧录/下载/刷到开发板上、再靠串口日志（printf）确认板上行为的任务，都应由它来处理——
包括"编译烧录一下"、"下载程序"、"烧完没反应"、"串口没输出/乱码/打印不对"、改完代码要"上板验证"。
即使你没有说出"闭环""调试""skill"这些词，只要任务是让代码跑到真板子上并得到反馈，就应该触发本 skill。

---

## 核心心法

**Agent 盯不了交互式终端。** Claude 无法像人一样盯着 GUI 串口助手或长时间挂一个 `read` 循环。
所以本 skill 的全部证据都通过「时间盒捕获成文件」获得，然后由 Agent 用 `Read` 读文件下结论——
整个 skill 就是围绕这一件事搭起来的。

对应地，**GDB 断点单步这类交互式人工调试不在本 skill 范围内。**

## 它能做什么（一句话版）

一条命令完成 4 步并给出结构化结论：

```
configure → build → flash → UART capture
      ↑ 任一阶段失败，立即停下并报告失败点与全部日志，Agent 直接跳去诊断
```

## 目录结构

```
stm32-debug-loop/
├── SKILL.md                        # skill 主入口，Claude Code 加载的就是这一份
├── scripts/                        # 自动化脚本（本 skill 自带，下文用 $S 表示该目录）
│   ├── find_tools.py               # 定位 cmake/ninja/GCC/Programmer_CLI，输出 JSON
│   ├── devloop.py                  # 一把梭：configure→build→flash→capture，结构化 JSON
│   ├── flash.py                    # 仅烧录：自动找 CLI 和最新 .elf，翻译失败原因
│   └── serial_capture.py           # 仅抓串口 N 秒存 UTF-8 文件（VID/PID 自动定位）
├── references/                     # 参考文档
│   ├── troubleshooting.md          # 按失败阶段走的排查决策树
│   └── project-facts.template.md   # 每工程一张的事实卡模板（串口号/波特率/已知坑）
└── evals/
    └── evals.json                  # 两组评估任务（真实烧录 + 静默故障诊断）
```

## 环境要求（兼容性）

| 依赖 | 说明 |
|---|---|
| 操作系统 | Windows 10/11 |
| Python | ≥ 3.10，且装有 `pyserial` |
| STM32CubeProgrammer CLI | 来自 STM32CubeCLT 或 STM32Cube VSCode 扩展的 bundles |
| 固件工程 | CMake + Ninja + arm-none-eabi-gcc，带 `CMakePresets.json` |
| 调试器 | ST-Link SWD 调试器，连接 COM 口串口 |

找不到 `python` 时用 `py`；工具的查找顺序见 [scripts/find_tools.py](scripts/find_tools.py)
（VSCode bundles → `C:\ST\STM32CubeCLT*` → 系统 PATH）。

## 脚本一览

下表 `$S` 指脚本所在目录的绝对路径（即本工程的 `scripts/`）。

| 脚本 | 用途 | 典型调用 |
|---|---|---|
| `find_tools.py` | 定位 cmake/ninja/GCC/Programmer_CLI(GDB server)，输出 JSON | `python "$S/find_tools.py"` |
| `devloop.py` | **一把梭**：configure→build→flash→capture，首个失败阶段停下并给出结构化 JSON | `python "$S/devloop.py" --project <工程目录> --seconds 4` |
| `flash.py` | 仅烧录（自动找 CLI 和最新 .elf，翻译失败原因） | `python "$S/flash.py" --elf xxx.elf` |
| `serial_capture.py` | 仅抓串口 N 秒存 UTF-8 文件（VID/PID 自动定位，端口被占给出提示） | `python "$S/serial_capture.py" --out uart.log --seconds 4 --ts` |

### 核心：一次闭环

```bash
python "$S/devloop.py" --project D:/path/to/project --seconds 4 --ts
```

最后一行输出 `=== DEVLOOP_RESULT === {...}`：

- `"stage": "build"` 且带 `errors[]` → 逐条修复（带 `file:line`），**只修第一条再重跑**，
  避免连锁误判；`undefined reference` 多半是源文件没进 CMakeLists。
- `"stage": "flash"` 带 `hint` → 按 hint 处理（SWD 接触 / 复位连接 mode=UR / 读保护 RDP）。
- `"stage": "capture"` 且 `silent: true` → 端口开了但零字节，进入「静默排查」（见 [references/troubleshooting.md](references/troubleshooting.md)）。
- 成功 → `Read` `uart.log`（路径在 artifacts 里），核对内容是否符合预期，再决定是否继续迭代。

build/flash/capture 的完整日志分别落在 `<工程>/build/devloop_last/{build,flash,uart}.log`，
`result.json` 是机器可读的全量结果。

分步版本用于只想做其中一段：编译只查错用 `--no-flash --no-capture`；烧过之后想再观察久一点，
单独跑 `serial_capture.py`，结束会自动释放端口。

## 与目标工程的关系

本 skill 是**通用工具**，运行在任意 STM32 固件工程之上。它依赖每个工程的一张「事实卡」：

1. 按优先级查找事实卡：`docs/debug-loop.md` → `debug-loop.md` → `README.md` 的调试章节 →
   `bsp/boards/<板型>/board.h`。
2. 事实卡记录：串口号 / VID:PID、波特率、预期开机输出、已知坑（quirks）。
3. 找到 → 按它来；没有 → 从 `.ioc`/README/原理图推断，向用户口头确认后，**替用户建一张**。
   模板见 [references/project-facts.template.md](references/project-facts.template.md)。

## 排查手册

任何阶段失败，先查 [references/troubleshooting.md](references/troubleshooting.md) 对应小节——
里面有按症状走的决策树（烧录失败分类表、串口静默排查、乱码定位、常见 GCC 报错对照），
以及引导你在事实卡里沉淀新坑的约定。

## 几条硬规矩（都是踩出来的）

1. **独占资源一次一路**：ST-Link 和 COM 口都是独占设备。开新一轮前确认没有别的进程在抓串口
   （VSCode 底部状态栏的 Serial Monitor 关掉）、上一个 GDB 会话已退出；否则 capture 会明确报 Access denied。
2. **DTR/RTS 默认不拉**：很多板子把复位接在 DTR 上，默认已是安全值；除非事实卡写了相反约定，不要加 `--dtr`。
3. **每轮都给用户看证据**：说"已完成"时附 `uart.log` 尾部关键行。
4. **烧录窗口耐心等**：devloop 内部已设超时（build 15min、flash 3min）。
5. printf 打浮点（`%f`）在 newlib-nano 下看不到输出？需链接 `-u _printf_float`，属常规改动。

## 评估（evals）

[evals/evals.json](evals/evals.json) 定义了两组端到端评估任务，用于验证本 skill 的真实效果：

| id | 场景 | 目标 |
|---|---|---|
| 0 | `add-led-blink-counter` | 实现 LED 250ms 闪烁 + 计数打印，构建→烧录→串口捕获，拿到递增的 `blink=N` 行 |
| 1 | `diagnose-boot-fault` | 排查"烧完毫无反应"：定位早期 HardFault，移除错误种子代码，重烧后恢复开机输出 |

评估在 `stm32-debug-loop-workspace/` 下跑，需要真实 ST-Link 与开发板连接。

## 如何安装为可用 skill

本目录自身即是一个 Claude Code skill，放在 `~/.claude/skills/stm32-debug-loop/`。
`SKILL.md` 的 frontmatter 声明了 `name` 与 `description` —— description 用于决定何时触发，
所以它在写命令（"编译烧录一下"）和策略（"上板验证"）上做了大量铺垫，务保需求匹配的会话能命中。

要让一个新会话命中它，只需让任务措辞贴近 description 所覆盖的场景即可。

## 贡献与开发

- 修改脚本后，建议在 `evals/` 下跑一遍真实闭环自测。
- 新踩的「这块板子/这个工程下次还会遇到」级别的坑，追加到对应工程根目录的 `debug-loop.md` 的 quirks 小节——
  这正是这个闭环越用越顺的原因。
